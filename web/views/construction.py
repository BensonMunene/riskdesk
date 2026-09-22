import numpy as np
import pandas as pd
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from analytics import backtest as bt
from analytics import optimize as opt
from analytics.covariance import estimate_cov
from analytics.returns import TRADING_DAYS, simple_returns, window
from analytics.risk import drawdown_series, risk_contributions
from core.models import OptimizationRun
from web.compare import compare, trade_table
from web.forms import BacktestForm, OptimizeForm, SizingForm
from web.services import asset_meta_frame, jsonable, limits_for, series_xy

from .common import chart, portfolio_context
from .trades import SESSION_KEY

HEDGE_CLASSES = {"ETF", "Fixed Income", "Commodity", "Currency"}


def _parse_views(text: str) -> list[dict]:
    views = []
    for line in (text or "").splitlines():
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            continue
        spec, ret = parts[0].upper(), float(parts[1])
        conf = float(parts[2]) if len(parts) > 2 else 0.5
        if "-" in spec:
            a, b = spec.split("-", 1)
            views.append({"assets": {a: 1.0, b: -1.0}, "return": ret, "confidence": conf})
        else:
            views.append({"assets": {spec: 1.0}, "return": ret, "confidence": conf})
    return views


def _universe(a, form):
    tickers = list(a.positions.index)
    if form.cleaned_data["universe"] == "holdings_plus":
        for t in form.cleaned_data["candidates"].replace(";", ",").split(","):
            t = t.strip().upper()
            if t and t in a.prices.columns and t not in tickers:
                tickers.append(t)
    return tickers


@login_required
def optimize(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    portfolio = ctx["portfolio"]
    # sensible defaults for this book: long/short books keep their net and gross, long-only books stay long-only
    has_short = bool((a.weights < 0).any())
    w_max = 0.25 if len(a.positions) < 12 else 0.10
    s = ctx["summary"]
    initial = {"w_max": w_max, "long_only": not has_short, "w_min": -w_max if has_short else 0.0,
               "budget": round(s["net_exposure"], 2), "gross_max": round(s["gross_exposure"] + 0.1, 2) if has_short else None}
    form = OptimizeForm(request.POST or None, initial=initial)
    result = None
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        tickers = _universe(a, form)
        rets = window(simple_returns(a.prices[tickers]), a.settings["lookback_days"]).dropna(axis=1, how="all")
        tickers = list(rets.columns)
        cov = estimate_cov(rets, a.settings["cov_method"], a.settings["ewma_halflife"]) * TRADING_DAYS
        rf = a.settings["risk_free_rate"]
        mu = opt.expected_returns(rets, cd["return_model"], market=a.benchmark_returns, rf=rf)
        meta = asset_meta_frame()
        groups = meta["sector"].reindex(tickers).fillna("Unclassified")
        has_equity = (a.positions["asset_class"] == "Equity").any()
        fixed = {}
        if cd["keep_hedges"] and has_equity:
            for t in tickers:
                if t in a.positions.index and a.positions.loc[t, "asset_class"] in HEDGE_CLASSES:
                    fixed[t] = float(a.weights[t])
        # budget: keep the same net exposure as today unless user overrides with long-only default
        cons = opt.Constraints(
            long_only=cd["long_only"], budget=cd["budget"], w_min=cd["w_min"], w_max=cd["w_max"],
            gross_max=cd["gross_max"], groups=groups,
            group_max={g: cd["sector_max"] for g in groups.unique()} if cd["sector_max"] else {},
            turnover_max=cd["turnover_max"], w0=a.weights.reindex(tickers).fillna(0.0),
            max_vol=cd["max_vol"], fixed=fixed,
        )
        kw = {"risk_aversion": cd["risk_aversion"], "target": cd["target"] or (0.08 if cd["method"] == "target_return" else 0.12)}
        if cd["method"] == "black_litterman":
            kw["views"] = _parse_views(cd["bl_views"])
            mcap = meta["market_cap"].reindex(tickers)
            kw["market_weights"] = mcap.fillna(mcap.median() if mcap.notna().any() else 1.0)
        try:
            res = opt.run(cd["method"], mu, cov, cons, rf=rf, **kw)
        except Exception as exc:  # noqa: BLE001
            res = None
            messages.error(request, f"Optimiser error: {exc}")
        if res is not None and res.ok:
            target_w = res.weights.reindex(tickers).fillna(0.0)
            trade_list = a.weights_to_shares(target_w)
            trades = {t: float(r["delta_shares"]) for t, r in trade_list.iterrows() if abs(r["delta_shares"]) >= 1}
            b = a.with_trades(trades, meta) if trades else a
            limits = limits_for(portfolio)
            cmp_ = compare(a, b, limits)
            rc_after = risk_contributions(target_w, cov / TRADING_DAYS)
            frontier = []
            if cd["method"] in ("max_sharpe", "mean_variance", "target_return", "target_vol", "min_variance", "black_litterman"):
                try:
                    mu_f = res.extra.get("posterior", mu) if cd["method"] == "black_litterman" else mu
                    frontier = opt.efficient_frontier(mu_f, cov, cons, 15, rf)
                except Exception:  # noqa: BLE001
                    frontier = []
            cur_w = a.weights.reindex(tickers).fillna(0.0)
            cur_vol = float(np.sqrt(cur_w @ cov @ cur_w))
            cur_ret = float(mu @ cur_w)
            asset_pts = {"labels": tickers, "vol": list(np.sqrt(np.diag(cov))), "ret": list(mu.reindex(tickers))}
            result = {
                "res": res, "tickers": tickers, "mu": mu, "cons_desc": cons.describe(),
                "weights": pd.DataFrame({"current": cur_w, "target": target_w, "delta": target_w - cur_w,
                                         "expected_return": mu.reindex(tickers), "vol": np.sqrt(np.diag(cov)),
                                         "risk_share_after": rc_after["pct"].reindex(tickers),
                                         "sector": groups}).sort_values("delta", key=np.abs, ascending=False),
                "trade_list": trade_list, "trades": trades, "compare": cmp_, "trade_rows": trade_table(a, trades),
                "current": {"vol": cur_vol, "ret": cur_ret, "sharpe": (cur_ret - rf) / cur_vol if cur_vol else None},
                "turnover": float((target_w - cur_w).abs().sum()) / 2.0,
                "bl": {"prior": res.extra.get("prior"), "posterior": res.extra.get("posterior"),
                       "shift": (res.extra.get("posterior") - res.extra.get("prior")).to_dict(),
                       "views": kw.get("views")} if cd["method"] == "black_litterman" else None,
                "charts": chart(
                    weights={"labels": tickers, "current": list(cur_w), "target": list(target_w)},
                    frontier={"vol": [p["vol"] for p in frontier], "ret": [p["return"] for p in frontier],
                              "opt": {"vol": res.vol, "ret": res.expected_return},
                              "cur": {"vol": cur_vol, "ret": cur_ret}, "assets": asset_pts},
                    rc={"labels": tickers, "before": list(a.rc["pct"].reindex(tickers).fillna(0.0)),
                        "after": list(rc_after["pct"].reindex(tickers))},
                ),
            }
            OptimizationRun.objects.create(
                portfolio=portfolio, method=cd["method"], created_by=request.user,
                params=jsonable({k: v for k, v in cd.items()}),
                result=jsonable({"weights": target_w.round(5).to_dict(), "expected_return": res.expected_return,
                                 "vol": res.vol, "sharpe": res.sharpe, "status": res.status}))
            if request.POST.get("action") == "send":
                request.session[SESSION_KEY] = {"trades": trades, "name": f"Optimiser: {opt.METHODS[cd['method']]}",
                                                "rationale": "; ".join(cons.describe())}
                return redirect("whatif", pk=pk)
        elif res is not None:
            messages.error(request, res.message or "Optimiser failed.")
    runs = portfolio.optimization_runs.all()[:8]
    ctx.update({"form": form, "result": result, "runs": runs, "methods": opt.METHODS, "return_models": opt.RETURN_MODELS})
    return render(request, "web/optimize.html", ctx)


@login_required
def sizing(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    form = SizingForm(request.POST or None)
    result = None
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        cov = a.cov_annual
        rf = a.settings["risk_free_rate"]
        scaled = opt.vol_target_scale(a.weights, cov, cd["target_vol"])
        mu = opt.expected_returns(a.window_returns, cd["return_model"], market=a.benchmark_returns, rf=rf)
        kelly = opt.kelly_weights(mu, cov, rf, cd["kelly_fraction"])
        # ERC over the equity sleeve keeping gross exposure of the sleeve
        eq = [t for t in a.positions.index if a.positions.loc[t, "asset_class"] == "Equity" and a.weights[t] > 0]
        erc = None
        if len(eq) >= 2:
            sub = cov.loc[eq, eq]
            r = opt.risk_parity(sub)
            if r.ok:
                sleeve_gross = float(a.weights[eq].sum())
                erc = pd.DataFrame({"current": a.weights[eq], "erc": r.weights * sleeve_gross,
                                    "current_risk_share": a.rc["pct"].reindex(eq)})
                erc["delta"] = erc["erc"] - erc["current"]
                erc = erc.sort_values("delta", key=np.abs, ascending=False)
        new_pos = None
        t = (cd["new_ticker"] or "").strip().upper()
        if t:
            if t not in a.prices.columns:
                messages.warning(request, f"{t} has no price history loaded.")
            else:
                new_pos = _size_new_position(a, t, cd["new_risk_share"])
        vol_rows = pd.DataFrame({"current": a.weights, "scaled": scaled["scaled_weights"].reindex(a.weights.index)})
        vol_rows["delta_shares"] = ((vol_rows["scaled"] - vol_rows["current"]) * a.nav / a.last_prices.reindex(vol_rows.index)).round()
        result = {"scaled": scaled, "kelly": kelly, "erc": erc, "new_pos": new_pos, "vol_rows": vol_rows, "mu": mu,
                  "kelly_rows": pd.DataFrame({"current": a.weights.reindex(kelly["weights"].index),
                                              "kelly": kelly["weights"], "expected_return": mu.reindex(kelly["weights"].index)}).sort_values("kelly", key=np.abs, ascending=False),
                  "charts": chart(erc={"labels": list(erc.index), "current": list(erc["current"]), "erc": list(erc["erc"])} if erc is not None else None)}
        if request.POST.get("action") in ("send_vol", "send_erc", "send_new") and result:
            trades = {}
            if request.POST["action"] == "send_vol":
                trades = {t: float(q) for t, q in vol_rows["delta_shares"].items() if abs(q) >= 1}
                name = f"Vol-target {cd['target_vol']:.0%}"
            elif request.POST["action"] == "send_erc" and erc is not None:
                trades = {t: float(np.round(d * a.nav / a.last_prices[t])) for t, d in erc["delta"].items() if abs(d * a.nav / a.last_prices[t]) >= 1}
                name = "Equal risk contribution (equity sleeve)"
            elif request.POST["action"] == "send_new" and new_pos:
                trades = {t: float(new_pos["shares"])}
                name = f"New position {t} at {cd['new_risk_share']:.0%} risk share"
            request.session[SESSION_KEY] = {"trades": trades, "name": name, "rationale": "Generated by position sizing tool"}
            return redirect("whatif", pk=pk)
    ctx.update({"form": form, "result": result})
    return render(request, "web/sizing.html", ctx)


def _size_new_position(a, ticker: str, target_share: float, max_weight: float = 0.5) -> dict:
    """Find the weight at which a new (or larger) position has `target_share` of portfolio vol."""
    px = float(a.last_prices[ticker])
    probe = a.with_trades({ticker: 1.0}, asset_meta_frame())  # ensures ticker is in the cov universe
    cov = probe.cov
    w = probe.weights.reindex(cov.index).fillna(0.0).astype(float)
    i = list(cov.index).index(ticker)
    c = cov.to_numpy()
    base_w = float(a.weights.get(ticker, 0.0))

    def share(x):
        ww = w.to_numpy().copy(); ww[i] = x
        var = ww @ c @ ww
        return ww[i] * (c @ ww)[i] / var if var > 0 else 0.0

    lo, hi = 0.0, max_weight
    if share(hi) < target_share:
        target_w = hi
    else:
        for _ in range(60):
            mid = (lo + hi) / 2
            if share(mid) < target_share:
                lo = mid
            else:
                hi = mid
        target_w = (lo + hi) / 2
    delta_w = target_w - base_w
    shares = float(np.round(delta_w * a.nav / px))
    after = a.with_trades({ticker: shares}, asset_meta_frame()) if shares else a
    return {"ticker": ticker, "target_weight": target_w, "current_weight": base_w, "delta_weight": delta_w,
            "shares": shares, "value": shares * px, "price": px, "achieved_share": float(after.rc["pct"].get(ticker, 0.0)),
            "vol_before": a.portfolio_vol, "vol_after": after.portfolio_vol,
            "standalone_vol": float(np.sqrt(c[i, i] * TRADING_DAYS)),
            "beta_to_book": float((c @ w.to_numpy())[i] / (w.to_numpy() @ c @ w.to_numpy())) if (w.to_numpy() @ c @ w.to_numpy()) > 0 else None}


@login_required
def backtest(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    form = BacktestForm(request.POST or None)
    results = None
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        tickers = list(a.positions.index)
        prices = a.prices[tickers].dropna(how="all")
        cons = opt.Constraints(long_only=cd["long_only"], w_max=cd["w_max"], w_min=0.0 if cd["long_only"] else -cd["w_max"])
        bench = a.benchmark_returns
        out = bt.compare(prices, cd["strategies"], current_weights=a.weights, rebalance=cd["rebalance"],
                         lookback=cd["lookback"], cost_bps=cd["cost_bps"], cons=cons,
                         cov_method=a.settings["cov_method"], rf=a.settings["risk_free_rate"],
                         start=cd["start"].isoformat() if cd["start"] else None, benchmark=bench)
        rows, curves, dds, final_w, weights_over_time = [], {}, {}, {}, {}
        for name, r in out.items():
            if isinstance(r, Exception):
                messages.warning(request, f"{bt.STRATEGIES.get(name, name)}: {r}")
                continue
            rows.append({"strategy": bt.STRATEGIES.get(name, name), "key": name, **r.stats})
            curves[name] = series_xy(r.equity - 1)
            dds[name] = series_xy(drawdown_series(r.returns))
            final_w[name] = r.weights.iloc[-1].round(4).to_dict()
            monthly = r.weights.resample("ME").last().dropna(how="all")
            weights_over_time[name] = {"x": [d.strftime("%Y-%m-%d") for d in monthly.index],
                                       "series": {t: [float(v) for v in monthly[t].round(4)] for t in monthly.columns}}
        if bench is not None and rows:
            idx = next(iter(out.values())).returns.index if not isinstance(next(iter(out.values())), Exception) else None
            if idx is not None:
                b = bench.reindex(idx).fillna(0.0)
                curves["benchmark"] = series_xy((1 + b).cumprod() - 1)
        results = {"rows": rows, "final_weights": final_w, "tickers": tickers,
                   "charts": chart(curves=curves, drawdowns=dds, labels={k: bt.STRATEGIES.get(k, k) for k in curves},
                                   bench_name=a.settings["benchmark"], weights=weights_over_time)}
    ctx.update({"form": form, "results": results, "strategies": bt.STRATEGIES})
    return render(request, "web/backtest.html", ctx)
