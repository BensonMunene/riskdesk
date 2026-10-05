"""Builder for the weekly client report.

Turns a client config (tickers, mandate, model settings), a price history and the
previous week's state into one dictionary that the report template renders and
that is saved as the week's state for next week's deltas. No Django here.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd

from . import backtest as bt
from . import concentration as conc
from . import optimize as opt
from . import risk as rk
from .covariance import cov_to_corr, estimate_cov
from .engine import PortfolioAnalyzer
from .returns import TRADING_DAYS, simple_returns
from .signals import TREND_DOWN, TREND_UP, make_trend_rule, stock_snapshot, trend_ls_weights

OBJECTIVES = [
    ("equal_weight", "Equal weight", "Same weight in every name; the no-view baseline."),
    ("min_variance", "Minimum variance", "The least volatile mix of these names."),
    ("risk_parity", "Risk parity", "Every name contributes the same amount of risk."),
    ("max_diversification", "Max diversification", "Maximises the diversification ratio (most cancelling of swings)."),
    ("max_sharpe", "Max Sharpe", "Best return per unit of risk using six-month shrunk expected returns."),
]


# ----------------------------------------------------------------------------- helpers

def _f(x):
    """float or None (NaN/inf/None -> None)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def to_jsonable(obj):
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, (float, np.floating)):
        return _f(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, dt.datetime, dt.date)):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, pd.Series):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, pd.DataFrame):
        return [to_jsonable(r) for r in obj.reset_index().to_dict(orient="records")]
    if isinstance(obj, (pd.Index, np.ndarray)):
        return [to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "as_dict"):
        return to_jsonable(obj.as_dict())
    return str(obj)


def sparkline_svg(s: pd.Series, w: int = 110, h: int = 26) -> str:
    s = s.dropna()
    if len(s) < 2:
        return ""
    y = s.to_numpy(dtype=float)
    lo, hi = float(y.min()), float(y.max())
    rng = (hi - lo) or 1.0
    pts = " ".join(f"{i * w / (len(y) - 1):.1f},{h - 2 - (v - lo) / rng * (h - 4):.1f}" for i, v in enumerate(y))
    color = "#198754" if y[-1] >= y[0] else "#dc3545"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-label="six-month price">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.4" points="{pts}"/></svg>')


def _pct(x, d=1):
    v = _f(x)
    return "n/a" if v is None else f"{v * 100:.{d}f}%"


def _money(x):
    v = _f(x)
    if v is None:
        return "n/a"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e6:
        return f"{sign}${v / 1e6:.2f}m"
    if v >= 1e3:
        return f"{sign}${v / 1e3:.0f}k"
    return f"{sign}${v:.0f}"


def mandate_limits(m: dict) -> list[dict]:
    rows = [
        ("gross_exposure", None, "lte", m.get("gross_max"), "Gross exposure"),
        ("net_exposure", None, "lte", m.get("net_max"), "Net exposure (max)"),
        ("net_exposure", None, "gte", m.get("net_min"), "Net exposure (min)"),
        ("position_weight", None, "lte", m.get("max_long"), "Largest position"),
        ("vol", None, "lte", m.get("vol_max"), "Volatility (hard cap)"),
        ("var", None, "lte", m.get("var_99_1d_max"), "1-day 99% VaR"),
        ("max_risk_share", None, "lte", m.get("max_risk_share"), "Largest single-name risk share"),
        ("sector_gross", None, "lte", m.get("sector_gross_max"), "Largest sector (gross)"),
        ("days_to_liquidate", None, "lte", m.get("days_to_liquidate_max"), "Days to liquidate (worst)"),
    ]
    out = []
    for i, (metric, scope, op, thr, label) in enumerate(rows, 1):
        if thr is None:
            continue
        out.append({"id": i, "metric": metric, "scope": scope, "operator": op, "threshold": float(thr), "label": label})
    return out


# ----------------------------------------------------------------------------- main builder

def build_weekly_report(cfg: dict, close: pd.DataFrame, volume: pd.DataFrame | None, meta: pd.DataFrame,
                        prev_state: dict | None = None, events: dict | None = None,
                        as_of: pd.Timestamp | None = None) -> dict:
    close = close.sort_index()
    if as_of is not None:
        close = close.loc[:as_of]
        volume = volume.loc[:as_of] if volume is not None else None
    as_of = close.index[-1]
    model, mandate = cfg.get("model", {}), cfg.get("mandate", {})
    rule_cfg = {"long_ma": 200, "allow_short": True, "hysteresis": 0.05, "vol_band": 0.30, "min_trade": 0.03, "rel_band": 0.25}
    rule_cfg.update(cfg.get("rule", {}))
    rule_kw = {k: rule_cfg[k] for k in ("long_ma", "allow_short", "hysteresis", "vol_band", "min_trade", "rel_band")}
    rule_kw["net_max"] = cfg.get("mandate", {}).get("net_max")
    lookback = int(model.get("lookback_days", 126))
    halflife = int(model.get("ewma_halflife", 30))
    cov_method = model.get("cov_method", "ewma")
    rf = float(model.get("risk_free_rate", 0.04))
    bench = cfg.get("benchmark", "SPY")
    nav = float(cfg.get("nav", 10_000_000))
    warnings: list[str] = []

    tickers = [t for t in cfg["tickers"] if t in close.columns and close[t].dropna().shape[0] > 60]
    missing = [t for t in cfg["tickers"] if t not in tickers]
    if missing:
        warnings.append(f"No usable price history for {', '.join(missing)}; excluded this week.")
    prev = prev_state or {}
    prev_w = pd.Series(prev.get("house_weights", {}), dtype=float).reindex(tickers).fillna(0.0) if prev else None
    week_start = close.index[-6] if len(close) > 6 else close.index[0]

    # ---------------------------------------------------------------- 1. stocks
    snap = stock_snapshot(close, volume, tickers, bench, lookback, halflife)
    rets_all = simple_returns(close)
    rets = rets_all[tickers].iloc[-lookback:]
    bench_rets = rets_all[bench] if bench in rets_all.columns else None
    bench_snap = stock_snapshot(close, volume, [bench], bench, lookback, halflife).iloc[0] if bench in close.columns else None
    stock_rows = []
    for t, r in snap.iterrows():
        ev = (events or {}).get(t) or {}
        row = {k: _f(v) if not isinstance(v, (str, bool)) else v for k, v in r.items()}
        row.update({
            "ticker": t, "name": str(meta["name"].get(t, t)) if "name" in meta.columns else t,
            "sector": str(meta["sector"].get(t, "")) if "sector" in meta.columns else "",
            "sparkline": sparkline_svg(close[t].iloc[-lookback:]),
            "next_earnings": ev.get("next_earnings"),
            "earnings_in_days": ev.get("days"),
            "prev_trend": (prev.get("stocks", {}).get(t, {}) or {}).get("trend"),
            "prev_vol": (prev.get("stocks", {}).get(t, {}) or {}).get("vol"),
        })
        stock_rows.append(row)

    movers = sorted(stock_rows, key=lambda x: (x["ret_1w"] or 0))
    stock_notes = []
    for x in movers[-2:][::-1]:
        stock_notes.append(f"{x['ticker']} led the week at {_pct(x['ret_1w'])} ({x['trend']}, RSI {x['rsi']:.0f}).")
    for x in movers[:2]:
        stock_notes.append(f"{x['ticker']} lagged at {_pct(x['ret_1w'])} ({x['trend']}, {_pct(x['dd_from_high'])} from its 12-month high).")
    for x in stock_rows:
        if x["crossed_ma50_1w"]:
            stock_notes.append(f"{x['ticker']} crossed its 50-day average this week and is now {x['trend'].lower()}.")
        if x["vol_change"] is not None and x["vol_change"] > 0.25:
            stock_notes.append(f"{x['ticker']}'s volatility jumped {_pct(x['vol_change'], 0)} week-on-week to {_pct(x['vol'], 0)}.")

    # ---------------------------------------------------------------- 2. correlation
    # Sizing and risk use the fast (EWMA) covariance; the "how they move together" page uses the
    # plain six-month correlation, which is more stable and matches its label.
    cov = estimate_cov(rets, cov_method, halflife)
    corr = rets.corr()
    n = len(tickers)
    off = ~np.eye(n, dtype=bool)
    avg_corr = float(corr.to_numpy()[off].mean()) if n > 1 else float("nan")
    corr_1m = rets.iloc[-21:].corr()
    avg_corr_1m = float(corr_1m.to_numpy()[off].mean()) if n > 1 else float("nan")
    eq_w = pd.Series(1.0 / n, index=tickers)
    enb_equal = conc.effective_number_of_bets(eq_w, rets.cov())
    pairs = [p for p in conc.pairwise_clusters(corr, eq_w, threshold=0.70)]
    avg_to_others = pd.Series({t: float(corr.loc[t].drop(t).mean()) for t in tickers}).sort_values()
    diversifiers = [{"ticker": t, "avg_corr": _f(v)} for t, v in avg_to_others.items()]
    prev_avg_corr = (prev.get("metrics") or {}).get("avg_corr")
    # rolling one-month average pairwise correlation over the lookback: the diversification regime
    roll_src = rets_all[tickers].iloc[-(lookback + 21):]
    roll_x, roll_y = [], []
    for i in range(21, len(roll_src) + 1):
        cm = roll_src.iloc[i - 21:i].corr().to_numpy()
        roll_x.append(roll_src.index[i - 1].strftime("%Y-%m-%d"))
        roll_y.append(_f(cm[off].mean()) if n > 1 else None)

    # ---------------------------------------------------------------- 3. construction: objectives
    cov_ann = cov * TRADING_DAYS
    return_model = model.get("return_model", "capm")
    if return_model == "capm":
        # Deliberately conservative: cash plus beta times a fixed equity premium. Six-month
        # averages would simply reward whatever rallied most recently.
        premium = float(model.get("equity_premium", 0.05))
        mu = pd.Series({t: rf + float(snap.loc[t, "beta"]) * premium if not np.isnan(snap.loc[t, "beta"]) else rf + premium for t in tickers})
        mu_note = f"Expected returns are CAPM-style: {rf:.0%} cash plus beta times a {premium:.0%} equity premium."
    else:
        mu = opt.expected_returns(rets, return_model, market=bench_rets, rf=rf)
        mu_note = "Expected returns are six-month averages shrunk halfway toward the cross-sectional mean."
    cap = float(model.get("objective_max_weight", 0.25))
    cons = opt.Constraints(long_only=True, w_max=cap)
    objectives = []
    for key, label, desc in OBJECTIVES:
        try:
            res = opt.run(key, mu, cov_ann, cons, rf=rf)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{label} optimiser failed: {exc}")
            continue
        if not res.ok:
            warnings.append(f"{label} optimiser: {res.message}")
            continue
        w = res.weights.reindex(tickers).fillna(0.0)
        objectives.append({
            "key": key, "label": label, "description": desc,
            "weights": to_jsonable(w.round(4)), "expected_return": _f(res.expected_return), "vol": _f(res.vol),
            "sharpe": _f(res.sharpe), "max_weight": _f(w.max()), "max_ticker": str(w.idxmax()),
            "enb": _f(conc.effective_number_of_bets(w, cov)), "n_active": int((w > 0.005).sum()),
        })

    # ---------------------------------------------------------------- 4. construction: house allocation
    px_win = close[tickers].iloc[-max(260, lookback + 10):]
    house = trend_ls_weights(
        px_win, cov, target_vol=float(mandate.get("target_vol", 0.15)), max_long=float(mandate.get("max_long", 0.20)),
        max_short=float(mandate.get("max_short", 0.10)), gross_max=float(mandate.get("gross_max", 1.5)),
        prev=prev_w, turnover_max=float(mandate.get("turnover_max_weekly", 0.40)) if prev_w is not None else None,
        overrides=cfg.get("house_overrides") or None, max_risk_share=mandate.get("max_risk_share"), **rule_kw)
    hw = house["weights"]
    last_px = close[tickers].iloc[-1]
    house_rows, trades = [], []
    for t in tickers:
        w_new = float(hw[t]); w_old = float(prev_w[t]) if prev_w is not None else 0.0
        d = w_new - w_old
        dollars = d * nav
        shares = int(np.trunc(dollars / float(last_px[t]))) if float(last_px[t]) > 0 else 0  # truncate so a cap is never exceeded
        row = {"ticker": t, "direction": "Long" if w_new > 0 else ("Short" if w_new < 0 else "Flat"),
               "reason": house["reasons"].get(t, ""), "weight": w_new, "prev_weight": w_old, "delta": d,
               "dollars": w_new * nav, "trade_dollars": dollars, "trade_shares": shares, "price": _f(last_px[t]),
               "trend": str(snap.loc[t, "trend"]), "vol": _f(snap.loc[t, "vol"])}
        house_rows.append(row)
        if abs(d) >= 0.0005:
            trades.append({**row, "side": "BUY" if d > 0 else "SELL"})
    trades.sort(key=lambda r: -abs(r["trade_dollars"]))
    turnover = float(sum(abs(r["delta"]) for r in house_rows))
    house_rows.sort(key=lambda r: -r["weight"])

    # ---------------------------------------------------------------- 5. risk of the house book
    active = hw[hw != 0]
    risk = None
    if len(active):
        pos = pd.DataFrame({"quantity": {t: float(np.trunc(active[t] * nav / float(last_px[t]))) for t in active.index}})
        for col in ("name", "sector", "asset_class", "industry", "country"):
            pos[col] = [str(meta[col].get(t, "")) if col in meta.columns else "" for t in pos.index]
        pos["asset_class"] = pos["asset_class"].replace("", "Equity")
        pos["sector"] = pos["sector"].replace("", "Unclassified")
        cash = nav - float((pos["quantity"] * last_px.reindex(pos.index)).sum())
        settings = {"lookback_days": lookback, "cov_method": cov_method, "ewma_halflife": halflife,
                    "var_confidence": float(model.get("var_confidence", 0.99)), "var_horizon_days": 1,
                    "risk_free_rate": rf, "benchmark": bench, "liquidity_participation": 0.20, "target_beta": None}
        a = PortfolioAnalyzer(close, volume, pos, cash, settings, f"{cfg.get('client', 'Client')} house book")
        s = a.summary()
        var5 = rk.historical_var(a.port_returns_window, settings["var_confidence"], 5)
        limits = a.check_limits(mandate_limits(mandate))
        insights = [i.as_dict() for i in a.insights(limits)]
        rc = a.rc
        fd = a.factor_decomposition
        ex = fd["exposures"]
        stress_h = sorted([d for d in a.stress_historical if d.get("available")], key=lambda d: d["total_pnl"])
        stress_p = sorted(a.stress_hypothetical, key=lambda d: d["total_pnl"])
        risk = {
            "summary": to_jsonable({k: s[k] for k in ("nav", "cash", "gross_exposure", "net_exposure", "long_exposure", "short_exposure",
                                                     "n_positions", "n_long", "n_short", "vol", "beta", "var", "cvar", "var_dollar",
                                                     "cvar_dollar", "sharpe", "max_drawdown", "current_drawdown")}),
            "var5": _f(var5.var), "var5_dollar": _f(var5.var * nav), "es5": _f(var5.cvar),
            "realised_vol": _f(a.performance.get("ann_vol")),
            "rc": [{"ticker": t, "weight": _f(r["weight"]), "pct": _f(r["pct"]), "standalone_vol": _f(r["standalone_vol"]),
                    "mcr": _f(r["mcr"]), "beta_to_port": _f(r["beta_to_port"]), "sector": str(r["sector"])} for t, r in rc.iterrows()],
            "sector_rc": [{"sector": sname, "gross": _f(r["gross"]), "net": _f(r["weight"]), "pct": _f(r["pct"])} for sname, r in a.sector_rc.iterrows()],
            "factors": [{"factor": f, "beta": _f(r["beta"]), "factor_vol": _f(r["factor_vol"]), "pct_var": _f(r["pct_of_total_var"])} for f, r in ex.iterrows()],
            "systematic_vol": _f(fd["systematic_vol"]), "idio_vol": _f(fd["idiosyncratic_vol"]), "systematic_share": _f(fd["systematic_share"]),
            "stress_hist": [{"name": d["name"], "pnl": _f(d["total_pnl"]), "dollar": _f(d["total_dollar"]), "bench": _f(d.get("benchmark_return"))} for d in stress_h],
            "stress_hypo": [{"name": d["name"], "pnl": _f(d["total_pnl"]), "dollar": _f(d["total_dollar"]),
                             "shocks": ", ".join(f"{k} {v:+.0%}" for k, v in d["shocks"].items())} for d in stress_p],
            "limits": to_jsonable(limits), "insights": to_jsonable(insights),
            "n_breach": sum(1 for l in limits if l["status"] == "breach"), "n_warn": sum(1 for l in limits if l["status"] == "warn"),
            "concentration": to_jsonable(a.concentration),
            "liquidity_worst": _f(a.liquidity["days_to_liquidate"].max()) if len(a.liquidity) else None,
            "warnings": a.all_warnings(),
        }
        warnings.extend(a.all_warnings())
    else:
        warnings.append("The trend rule produced no active position this week (every name is flat); the risk section is skipped.")

    # ---------------------------------------------------------------- 6. evidence: walk-forward backtest
    bt_cfg = cfg.get("backtest", {})
    years = float(bt_cfg.get("years", 2))
    start = (as_of - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
    rule = make_trend_rule(target_vol=float(mandate.get("target_vol", 0.15)), max_long=float(mandate.get("max_long", 0.20)),
                           max_short=float(mandate.get("max_short", 0.10)), gross_max=float(mandate.get("gross_max", 1.5)),
                           turnover_max=float(mandate.get("turnover_max_weekly", 0.40)), cov_lookback=lookback, halflife=halflife,
                           max_risk_share=mandate.get("max_risk_share"), **rule_kw)
    strategies = {"House rule (trend long/short)": rule, "Equal weight (long-only)": "equal_weight", "Risk parity (long-only)": "risk_parity"}
    results = bt.compare(close[tickers], strategies, rebalance=bt_cfg.get("rebalance", "W"), lookback=252,
                         cost_bps=float(bt_cfg.get("cost_bps", 10)), cons=opt.Constraints(long_only=True, w_max=cap),
                         cov_method=cov_method, rf=rf, start=start, benchmark=bench_rets)
    bt_rows, curves, yearly = [], {}, {}
    idx = None
    for label, r in results.items():
        if isinstance(r, Exception):
            warnings.append(f"Backtest {label}: {r}")
            continue
        st = r.stats
        bt_rows.append({"label": label, "total": _f(st["total_return"]), "ann_return": _f(st["ann_return"]), "ann_vol": _f(st["ann_vol"]),
                        "sharpe": _f(st["sharpe"]), "sortino": _f(st.get("sortino")), "max_dd": _f(st["max_drawdown"]), "beta": _f(st.get("beta")),
                        "turnover": _f(st["annual_turnover"]), "avg_gross": _f(st["avg_gross"]), "worst_day": _f(st["worst_day"]),
                        "hit_rate": _f(st.get("hit_rate"))})
        wk = (r.equity - 1.0).resample("W-FRI").last().dropna()
        curves[label] = {"x": [d.strftime("%Y-%m-%d") for d in wk.index], "y": [_f(v) for v in wk.values]}
        yearly[label] = {str(y): _f(v) for y, v in r.returns.groupby(r.returns.index.year).apply(lambda s: (1 + s).prod() - 1).items()}
        idx = r.returns.index if idx is None else idx
    if bench_rets is not None and idx is not None:
        b = bench_rets.reindex(idx).fillna(0.0)
        wk = ((1 + b).cumprod() - 1.0).resample("W-FRI").last().dropna()
        curves[bench] = {"x": [d.strftime("%Y-%m-%d") for d in wk.index], "y": [_f(v) for v in wk.values]}
        bs = rk.performance_stats(b, rf=rf)
        bench_label = f"{bench} (buy and hold)"
        bt_rows.append({"label": bench_label, "total": _f(bs["total_return"]), "ann_return": _f(bs["ann_return"]),
                        "ann_vol": _f(bs["ann_vol"]), "sharpe": _f(bs["sharpe"]), "sortino": _f(bs.get("sortino")), "max_dd": _f(bs["max_drawdown"]),
                        "beta": 1.0, "turnover": 0.0, "avg_gross": 1.0, "worst_day": _f(bs["worst_day"]), "hit_rate": _f(bs.get("hit_rate"))})
        yearly[bench_label] = {str(y): _f(v) for y, v in b.groupby(b.index.year).apply(lambda s: (1 + s).prod() - 1).items()}
    years_list = sorted({y for d in yearly.values() for y in d})

    # ---------------------------------------------------------------- 7. watch list
    watch = []
    for x in stock_rows:
        if x["px_vs_ma50"] is not None and abs(x["px_vs_ma50"]) < 0.03:
            watch.append({"ticker": x["ticker"], "kind": "trend", "text": f"within {_pct(abs(x['px_vs_ma50']))} of its 50-day average; the direction signal could flip"})
        if x["rsi"] is not None and x["rsi"] >= 70:
            watch.append({"ticker": x["ticker"], "kind": "momentum", "text": f"RSI {x['rsi']:.0f}: overbought, short-term pullback risk"})
        if x["rsi"] is not None and x["rsi"] <= 30:
            watch.append({"ticker": x["ticker"], "kind": "momentum", "text": f"RSI {x['rsi']:.0f}: oversold, bounce risk for any short"})
        if x["vol_change"] is not None and x["vol_change"] > 0.25:
            watch.append({"ticker": x["ticker"], "kind": "volatility", "text": f"volatility up {_pct(x['vol_change'], 0)} on the week to {_pct(x['vol'], 0)}"})
        if x.get("earnings_in_days") is not None and 0 <= x["earnings_in_days"] <= 14:
            watch.append({"ticker": x["ticker"], "kind": "event", "text": f"reports earnings on {x['next_earnings']} ({x['earnings_in_days']} days): expect a gap move"})
    if not math.isnan(avg_corr_1m) and not math.isnan(avg_corr) and avg_corr_1m - avg_corr > 0.15:
        watch.append({"ticker": "Book", "kind": "regime", "text": f"one-month average correlation {avg_corr_1m:.2f} is well above the six-month {avg_corr:.2f}: diversification is weakening"})
    if risk:
        var_lim = next((l for l in risk["limits"] if l["metric"] == "var"), None)
        if var_lim and var_lim.get("utilization") and var_lim["utilization"] > 0.8:
            watch.append({"ticker": "Book", "kind": "risk", "text": f"VaR is at {var_lim['utilization'] * 100:.0f}% of its limit"})

    # ---------------------------------------------------------------- 8. deltas vs last week and draft commentary
    prev_m = prev.get("metrics") or {}
    risk_deltas = []
    if risk:
        for key, label, fmt in (("vol", "Volatility", "pct"), ("var", "1-day 99% VaR", "pct"), ("var5", "1-week 99% VaR", "pct"),
                                ("beta", f"Beta to {bench}", "num"), ("gross", "Gross exposure", "pct"), ("net", "Net exposure", "pct"),
                                ("avg_corr", "Average correlation", "num")):
            now = {"vol": risk["summary"]["vol"], "var": risk["summary"]["var"], "var5": risk["var5"], "beta": risk["summary"]["beta"],
                   "gross": risk["summary"]["gross_exposure"], "net": risk["summary"]["net_exposure"], "avg_corr": _f(avg_corr)}[key]
            risk_deltas.append({"label": label, "now": now, "prev": prev_m.get(key), "fmt": fmt,
                                "delta": (now - prev_m[key]) if (now is not None and prev_m.get(key) is not None) else None})

    n_up = sum(1 for x in stock_rows if x["trend"] == TREND_UP)
    n_down = sum(1 for x in stock_rows if x["trend"] == TREND_DOWN)
    draft_view = [f"{n_up} of the {len(stock_rows)} names are in uptrends and {n_down} in confirmed downtrends."]
    if not math.isnan(avg_corr):
        draft_view.append(f"Average pairwise correlation is {avg_corr:.2f} over six months and {avg_corr_1m:.2f} over the last month, "
                          + ("so the names are moving more as one group." if avg_corr_1m > avg_corr + 0.1 else "so diversification between the names is intact."))
    draft_view.append(f"The house book is {house['n_long']} long, {house['n_short']} short, {house['gross']:.0%} gross and {house['net']:+.0%} net, "
                      f"built to a {float(mandate.get('target_vol', 0.15)):.0%} volatility target.")
    if risk:
        top = risk["rc"][0] if risk["rc"] else None
        if top:
            draft_view.append(f"{top['ticker']} is the largest risk at {_pct(top['pct'], 0)} of book volatility on a {_pct(top['weight'])} weight.")
        if risk["stress_hist"]:
            w0 = risk["stress_hist"][0]
            draft_view.append(f"The worst historical replay is {w0['name']} at {_pct(w0['pnl'])} ({_money(w0['dollar'])}).")
        if risk["n_breach"]:
            draft_view.append(f"{risk['n_breach']} mandate limit(s) would be breached; see the risk page.")
        else:
            draft_view.append("Every mandate limit is respected.")
    actions = []
    for tr in trades[:5]:
        actions.append(f"{tr['side']} {abs(tr['trade_shares']):,} {tr['ticker']} ({_money(abs(tr['trade_dollars']))}): "
                       f"{'open' if tr['prev_weight'] == 0 else 'close' if tr['weight'] == 0 else 'resize'} to {tr['weight']:+.1%}; {tr['reason']}.")
    if not trades:
        actions.append("No trades: every name is within its no-trade band this week.")
    for wv in watch[:3]:
        actions.append(f"Watch {wv['ticker']}: {wv['text']}.")
    cm = cfg.get("commentary") or {}
    commentary = {
        "house_view": cm.get("house_view") or " ".join(draft_view),
        "house_view_is_draft": not bool(cm.get("house_view")),
        "allocation": cm.get("allocation") or ("The allocation follows the trend rule: " + "; ".join(house["steps"]) + "."),
        "allocation_is_draft": not bool(cm.get("allocation")),
        "stocks": cm.get("stocks") or {},
        "stock_notes": stock_notes,
    }

    # ---------------------------------------------------------------- 9. charts
    house_sorted = sorted(house_rows, key=lambda r: r["weight"])
    charts = to_jsonable({
        "corr": {"labels": tickers, "z": corr.round(2).values.tolist()},
        "objectives": {"labels": tickers, "series": [{"name": o["label"], "values": [o["weights"][t] for t in tickers]} for o in objectives]},
        "house": {"labels": [r["ticker"] for r in house_sorted], "weights": [r["weight"] for r in house_sorted],
                  "prev": [r["prev_weight"] for r in house_sorted]},
        "rc": {"labels": [r["ticker"] for r in risk["rc"]], "pct": [r["pct"] for r in risk["rc"]], "weight": [r["weight"] for r in risk["rc"]]} if risk else None,
        "factors": {"labels": [r["factor"] for r in risk["factors"]], "beta": [r["beta"] for r in risk["factors"]]} if risk else None,
        "stress": {"labels": [d["name"] for d in risk["stress_hist"]] + [d["name"] for d in risk["stress_hypo"][:4]],
                   "pnl": [d["pnl"] for d in risk["stress_hist"]] + [d["pnl"] for d in risk["stress_hypo"][:4]]} if risk else None,
        "backtest": {"curves": curves},
        "returns": {"labels": tickers, "ret_1w": [snap.loc[t, "ret_1w"] for t in tickers], "ret_1m": [snap.loc[t, "ret_1m"] for t in tickers]},
        "corr_roll": {"x": roll_x, "y": roll_y},
    })

    # ---------------------------------------------------------------- 10. assemble + state
    report = {
        "meta": {
            "client": cfg.get("client", "Client"), "slug": cfg.get("slug", "client"), "analyst": cfg.get("analyst", ""),
            "as_of": as_of.strftime("%Y-%m-%d"), "as_of_long": as_of.strftime("%d %B %Y"), "week_start": week_start.strftime("%d %b"),
            "week_end": as_of.strftime("%d %b %Y"), "tickers": tickers, "benchmark": bench, "nav": nav, "nav_note": cfg.get("nav_note", ""),
            "horizon": cfg.get("horizon", ""), "lookback": lookback, "cov_method": cov_method, "halflife": halflife,
            "mandate": mandate, "model": model, "rule": rule_cfg, "first_report": prev_w is None, "prev_date": prev.get("as_of"),
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
        "warnings": warnings,
        "stocks": {"rows": stock_rows, "benchmark": to_jsonable({k: _f(v) if not isinstance(v, (str, bool)) else v for k, v in bench_snap.items()}) if bench_snap is not None else None},
        "correlation": {"avg_corr": _f(avg_corr), "avg_corr_1m": _f(avg_corr_1m), "prev_avg_corr": prev_avg_corr, "enb_equal": _f(enb_equal),
                        "pairs": to_jsonable(pairs), "diversifiers": diversifiers},
        "construction": {"objectives": objectives, "cap": cap, "mu_note": mu_note, "house": {"rows": house_rows, "steps": house["steps"], "vol": _f(house["vol"]),
                         "gross": _f(house["gross"]), "net": _f(house["net"]), "n_long": house["n_long"], "n_short": house["n_short"],
                         "turnover": turnover, "trades": trades, "overrides": cfg.get("house_overrides") or {}}},
        "risk": risk,
        "evidence": {"rows": bt_rows, "start": start, "years": years, "rebalance": bt_cfg.get("rebalance", "W"), "cost_bps": bt_cfg.get("cost_bps", 10),
                     "yearly": yearly, "years_list": years_list},
        "watch": watch,
        "actions": actions,
        "deltas": {"risk": risk_deltas},
        "commentary": commentary,
        "charts": charts,
    }
    report["state"] = {
        "as_of": as_of.strftime("%Y-%m-%d"),
        "house_weights": to_jsonable(hw.round(4)),
        "objective_weights": {o["key"]: o["weights"] for o in objectives},
        "metrics": {"vol": risk["summary"]["vol"] if risk else None, "var": risk["summary"]["var"] if risk else None,
                    "var5": risk["var5"] if risk else None, "beta": risk["summary"]["beta"] if risk else None,
                    "gross": risk["summary"]["gross_exposure"] if risk else None, "net": risk["summary"]["net_exposure"] if risk else None,
                    "avg_corr": _f(avg_corr)},
        "stocks": {x["ticker"]: {"trend": x["trend"], "vol": x["vol"], "price": x["price"]} for x in stock_rows},
    }
    return report
