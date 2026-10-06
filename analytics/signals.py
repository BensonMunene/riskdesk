"""Short-horizon signals for the weekly client report.

Trend state, momentum, RSI, volatility regime, and the trend-aware long/short
sizing rule used for the house allocation. Everything here works on a wide
DataFrame of adjusted closes (rows = dates, columns = tickers).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .covariance import ewma_cov
from .returns import TRADING_DAYS, simple_returns

TREND_UP, TREND_DOWN, TREND_MIXED = "Uptrend", "Downtrend", "Mixed"


# ----------------------------------------------------------------------------- per-stock signals

def rsi(close: pd.Series, n: int = 14) -> float:
    """Relative Strength Index (Wilder smoothing). >70 overbought, <30 oversold."""
    d = close.dropna().diff().dropna()
    if len(d) < n + 1:
        return float("nan")
    up = d.clip(lower=0.0)
    dn = -d.clip(upper=0.0)
    au = up.ewm(alpha=1.0 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    last_ad = float(ad.iloc[-1])
    if last_ad == 0:
        return 100.0
    rs = float(au.iloc[-1]) / last_ad
    return float(100.0 - 100.0 / (1.0 + rs))


def trend_state(px: float, ma50: float, ma200: float) -> str:
    if np.isnan(ma200):
        return TREND_MIXED
    if px > ma50 and ma50 > ma200:
        return TREND_UP
    if px < ma50 and ma50 < ma200:
        return TREND_DOWN
    return TREND_MIXED


def window_return(s: pd.Series, n: int) -> float:
    s = s.dropna()
    if len(s) <= n:
        return float("nan")
    return float(s.iloc[-1] / s.iloc[-n - 1] - 1.0)


def ytd_return(s: pd.Series) -> float:
    s = s.dropna()
    if s.empty:
        return float("nan")
    start = s[s.index < pd.Timestamp(s.index[-1].year, 1, 1)]
    if start.empty:
        return float("nan")
    return float(s.iloc[-1] / start.iloc[-1] - 1.0)


def ewma_vol(returns: pd.Series, halflife: int = 30, periods: int = TRADING_DAYS) -> float:
    r = returns.dropna()
    if len(r) < 10:
        return float("nan")
    var = r.ewm(halflife=halflife).var().iloc[-1]
    return float(np.sqrt(var * periods))


def stock_snapshot(close: pd.DataFrame, volume: pd.DataFrame | None, tickers: list[str], benchmark: str,
                   lookback: int = 126, halflife: int = 30) -> pd.DataFrame:
    """One row per ticker with the numbers a short-horizon trader looks at every week."""
    rows = []
    rets_all = simple_returns(close)
    bench = rets_all[benchmark] if benchmark in rets_all.columns else None
    for t in tickers:
        if t not in close.columns:
            continue
        s = close[t].dropna()
        r = rets_all[t].dropna()
        rw = r.iloc[-lookback:]
        px = float(s.iloc[-1])
        ma20 = float(s.rolling(20).mean().iloc[-1]) if len(s) >= 20 else float("nan")
        ma50 = float(s.rolling(50).mean().iloc[-1]) if len(s) >= 50 else float("nan")
        ma200 = float(s.rolling(200).mean().iloc[-1]) if len(s) >= 200 else float("nan")
        ma50_prev = float(s.rolling(50).mean().iloc[-6]) if len(s) >= 56 else float("nan")
        px_prev = float(s.iloc[-6]) if len(s) >= 6 else float("nan")
        vol_now = ewma_vol(r, halflife)
        vol_prev = ewma_vol(r.iloc[:-5], halflife) if len(r) > 15 else float("nan")
        beta = float("nan")
        if bench is not None:
            b = bench.reindex(rw.index).dropna()
            rr = rw.reindex(b.index)
            if len(b) > 20 and b.var() > 0:
                beta = float(rr.cov(b) / b.var())
        high_12m = float(s.iloc[-252:].max())
        adv = float((volume[t].iloc[-30:] * close[t].iloc[-30:]).mean()) if volume is not None and t in volume.columns else float("nan")
        week = r.iloc[-5:]
        rows.append({
            "ticker": t, "price": px,
            "ret_1w": window_return(s, 5), "ret_1m": window_return(s, 21), "ret_3m": window_return(s, 63),
            "ret_ytd": ytd_return(s), "ret_12m": window_return(s, 252),
            "vol": vol_now, "vol_prev": vol_prev, "vol_change": (vol_now / vol_prev - 1.0) if vol_prev and vol_prev > 0 else float("nan"),
            "beta": beta, "dd_from_high": px / high_12m - 1.0 if high_12m > 0 else float("nan"),
            "adv_dollar": adv,
            "ma20": ma20, "ma50": ma50, "ma200": ma200,
            "px_vs_ma50": px / ma50 - 1.0 if ma50 else float("nan"),
            "px_vs_ma200": px / ma200 - 1.0 if ma200 and not np.isnan(ma200) else float("nan"),
            "ma50_vs_ma200": ma50 / ma200 - 1.0 if ma200 else float("nan"),
            "trend": trend_state(px, ma50, ma200),
            "trend_prev": trend_state(px_prev, ma50_prev, float(s.rolling(200).mean().iloc[-6]) if len(s) >= 206 else float("nan")),
            "crossed_ma50_1w": bool(np.sign(px - ma50) != np.sign(px_prev - ma50_prev)) if not np.isnan(ma50_prev) else False,
            "rsi": rsi(s), "max_move_1w": float(week.abs().max()) if len(week) else float("nan"),
            "worst_day_1w": float(week.min()) if len(week) else float("nan"),
        })
    df = pd.DataFrame(rows).set_index("ticker")
    return df


# ----------------------------------------------------------------------------- house allocation rule

def trend_ls_weights(px: pd.DataFrame, cov: pd.DataFrame, target_vol: float = 0.15, max_long: float = 0.20,
                     max_short: float = 0.10, gross_max: float = 1.5, prev: pd.Series | None = None,
                     turnover_max: float | None = None, overrides: dict | None = None,
                     min_trade: float = 0.03, rel_band: float = 0.25, vol_band: float = 0.25,
                     max_risk_share: float | None = None, hysteresis: float = 0.02,
                     long_ma: int = 50, allow_short: bool = True, net_max: float | None = None,
                     periods: int = TRADING_DAYS) -> dict:
    """Trend-aware long/short equal-risk allocation.

    1. Direction: long if price > its `long_ma`-day average; short (if `allow_short`) when
       price < 50-day average AND 50-day < 200-day (confirmed downtrend); otherwise flat.
       With `hysteresis`, an existing position is only closed once price crosses
       `hysteresis` beyond the average, and a new one only opened once it is
       `hysteresis` beyond it, which stops a name hovering at its average from
       flipping every week.
    2. Size: inverse-volatility so each active name carries similar risk.
    3. Scale the book to the target volatility, respecting per-name and gross caps.
    4. Risk-share cap: trim any name whose Euler share of book volatility exceeds
       `max_risk_share`, then rescale.
    5. Stability versus last week: a name keeping its direction is only resized when the
       target moves by more than max(`min_trade`, `rel_band` x current weight); the whole
       book is only rescaled when its volatility drifts outside ±`vol_band` of target.
       Direction changes always trade.
    6. Limit one-way turnover versus the previous allocation.
    7. Apply analyst overrides (exact weights for named tickers).
    """
    tickers = list(px.columns)
    last = px.iloc[-1]
    ma_long = px.rolling(long_ma).mean().iloc[-1] if len(px) >= long_ma else pd.Series(np.nan, index=tickers)
    ma50 = px.rolling(50).mean().iloc[-1]
    ma200 = px.rolling(200).mean().iloc[-1] if len(px) >= 200 else pd.Series(np.nan, index=tickers)
    c = cov.reindex(index=tickers, columns=tickers).fillna(0.0)
    cm = c.to_numpy()
    vols = pd.Series(np.sqrt(np.diag(cm)) * np.sqrt(periods), index=tickers).replace(0, np.nan)
    direction = pd.Series(0, index=tickers, dtype=int)
    reasons = {}
    prev_dir = np.sign(prev.reindex(tickers).fillna(0.0)).astype(int) if prev is not None else pd.Series(0, index=tickers)
    h = hysteresis or 0.0
    for t in tickers:
        p = float(last[t])
        ml = float(ma_long[t]) if not np.isnan(ma_long[t]) else np.nan
        m50 = float(ma50[t]) if not np.isnan(ma50[t]) else np.nan
        m200 = float(ma200[t]) if not np.isnan(ma200[t]) else np.nan
        if np.isnan(ml) or np.isnan(m50):
            reasons[t] = "insufficient history"
            continue
        gap = p / ml - 1.0                      # distance from the long-signal average
        gap50 = p / m50 - 1.0
        downtrend = allow_short and (not np.isnan(m200)) and m50 < m200 and p < m200
        held = int(prev_dir[t])
        if held == 1 and gap > -h:
            direction[t] = 1
            reasons[t] = f"holding long: price {gap:+.1%} vs {long_ma}-day average (exit below {-h:.0%})"
        elif held == -1 and gap50 < h:
            direction[t] = -1
            reasons[t] = f"holding short: price {gap50:+.1%} vs 50-day average (cover above {h:+.0%})"
        elif gap > h:
            direction[t] = 1
            reasons[t] = f"price {gap:+.1%} above {long_ma}-day average"
        elif gap50 < -h and downtrend:
            direction[t] = -1
            reasons[t] = f"price {gap50:+.1%} below 50-day, 50-day {m50 / m200 - 1:+.1%} below 200-day"
        elif gap < -h:
            reasons[t] = f"below {long_ma}-day average but no confirmed downtrend: flat"
        else:
            reasons[t] = f"within {h:.0%} of its {long_ma}-day average: no new position"
    inv = (1.0 / vols).fillna(0.0) * (direction != 0)
    w = pd.Series(0.0, index=tickers) if inv.sum() == 0 else direction * inv / inv.sum()

    def port_vol(x: pd.Series) -> float:
        v = x.to_numpy(dtype=float)
        return float(np.sqrt(max(v @ cm @ v, 0.0) * periods))

    def risk_shares(x: pd.Series) -> pd.Series:
        v = x.to_numpy(dtype=float)
        var = float(v @ cm @ v)
        return pd.Series(v * (cm @ v) / var, index=tickers) if var > 0 else pd.Series(0.0, index=tickers)

    # per-name caps; the risk-share loop below tightens them for offenders so a rescale cannot undo a trim
    hi = pd.Series(max_long, index=tickers, dtype=float)
    lo = pd.Series(-max_short, index=tickers, dtype=float)

    def scale_to_target(x: pd.Series) -> pd.Series:
        for _ in range(6):
            pv = port_vol(x)
            if pv > 0:
                x = x * (target_vol / pv)
            gross = float(x.abs().sum())
            if gross > gross_max:
                x = x * (gross_max / gross)
            net = float(x.sum())
            if net_max is not None and net > net_max > 0:
                x = x * (net_max / net)
            x = x.clip(lower=lo, upper=hi)
        return x

    steps = [f"{int((direction == 1).sum())} long, {int((direction == -1).sum())} short, {int((direction == 0).sum())} flat"]
    w = scale_to_target(w)
    if max_risk_share:
        for _ in range(12):
            rs = risk_shares(w)
            over = (rs > max_risk_share) & (w != 0)
            if not over.any():
                break
            shrink = (max_risk_share / rs[over]) * 0.95
            hi[over] = np.minimum(hi[over], (w[over] * shrink).clip(lower=0))
            lo[over] = np.maximum(lo[over], (w[over] * shrink).clip(upper=0))
            w[over] = w[over] * shrink
            w = scale_to_target(w)
        steps.append(f"no name above {max_risk_share:.0%} of book risk (largest {risk_shares(w).max():.0%})")
    steps.append(f"scaled to {port_vol(w):.1%} volatility (target {target_vol:.0%}), gross {w.abs().sum():.0%}")
    if prev is not None:
        prev = prev.reindex(tickers).fillna(0.0)
        same_side = (np.sign(w) == np.sign(prev)) & (prev != 0)
        band = np.maximum(min_trade, rel_band * prev.abs())
        keep = same_side & ((w - prev).abs() < band)
        if keep.any():
            w[keep] = prev[keep]
        pv = port_vol(w)
        if pv > 0 and (pv > target_vol * (1 + vol_band) or pv < target_vol * (1 - vol_band)):
            w = scale_to_target(w)
            steps.append(f"book volatility had drifted to {pv:.1%}, outside the ±{vol_band:.0%} band; rescaled to target")
        else:
            steps.append(f"{int(keep.sum())} name(s) left at last week's size (within the no-trade band); book volatility {pv:.1%}")
    if prev is not None and turnover_max is not None:
        turnover = float((w - prev).abs().sum())
        if turnover > turnover_max > 0:
            w = prev + (w - prev) * (turnover_max / turnover)
            steps.append(f"turnover {turnover:.0%} capped at {turnover_max:.0%}: moved part-way toward the target")
        else:
            steps.append(f"turnover {turnover:.0%} within the {turnover_max:.0%} weekly limit")
    if overrides:
        for t, v in overrides.items():
            if t in w.index:
                w[t] = float(v)
                reasons[t] = f"analyst override: {float(v):+.1%}"
        steps.append(f"analyst overrides applied: {', '.join(overrides)}")
    # final mandate guard: keeping last week's sizes and adding a new name can push the book over
    # its gross or net cap, so scale everything pro rata (new positions are funded by the others)
    gross, net = float(w.abs().sum()), float(w.sum())
    k = 1.0
    if gross > gross_max > 0:
        k = min(k, gross_max / gross)
    if net_max is not None and net > net_max > 0:
        k = min(k, net_max / net)
    if k < 1.0 - 1e-9:
        w = w * k
        steps.append(f"gross would have been {gross:.0%}: every position scaled by {k:.2f} to respect the {gross_max:.0%} cap")
    w = w.round(4)
    return {"weights": w, "direction": direction, "reasons": reasons, "steps": steps,
            "vol": port_vol(w), "gross": float(w.abs().sum()), "net": float(w.sum()),
            "n_long": int((w > 0).sum()), "n_short": int((w < 0).sum())}


def make_trend_rule(target_vol=0.15, max_long=0.20, max_short=0.10, gross_max=1.5, turnover_max=0.40,
                    cov_lookback=126, halflife=30, max_risk_share=None, hysteresis=0.02, vol_band=0.25,
                    min_trade=0.03, rel_band=0.25, long_ma=50, allow_short=True, net_max=None):
    """Factory returning a backtest-compatible rule: (hist_returns, current_weights) -> weights."""
    def rule(hist: pd.DataFrame, current: pd.Series) -> pd.Series:
        px = (1.0 + hist.fillna(0.0)).cumprod()
        cov = ewma_cov(hist.iloc[-cov_lookback:], halflife=halflife)
        res = trend_ls_weights(px, cov, target_vol, max_long, max_short, gross_max,
                               prev=current, turnover_max=turnover_max, max_risk_share=max_risk_share,
                               hysteresis=hysteresis, vol_band=vol_band, min_trade=min_trade, rel_band=rel_band,
                               long_ma=long_ma, allow_short=allow_short, net_max=net_max)
        return res["weights"]
    rule.__name__ = "trend_ls"
    return rule
