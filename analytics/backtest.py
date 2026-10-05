"""Walk-forward backtests of allocation rules.

At each rebalance date the rule sees only trailing data (no look-ahead), sets
target weights, pays transaction costs on turnover, then weights drift with
prices until the next rebalance.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import optimize as opt
from .covariance import estimate_cov
from .returns import TRADING_DAYS, simple_returns
from .risk import performance_stats

STRATEGIES = {
    "current": "Hold current weights (rebalanced)",
    "buy_hold": "Buy and hold current weights (never rebalance)",
    "equal_weight": "Equal weight",
    "inverse_vol": "Inverse volatility",
    "min_variance": "Minimum variance",
    "risk_parity": "Risk parity (ERC)",
    "max_diversification": "Maximum diversification",
    "max_sharpe": "Maximum Sharpe (shrunk means)",
    "vol_target": "Current weights, scaled to a 10% vol target",
}

REBALANCE_FREQ = {"W": "Weekly", "M": "Monthly", "Q": "Quarterly", "A": "Annually"}


@dataclass
class BacktestResult:
    strategy: str
    returns: pd.Series
    equity: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    stats: dict
    params: dict = field(default_factory=dict)


def _rebalance_dates(index: pd.DatetimeIndex, freq: str) -> list[pd.Timestamp]:
    rule = {"W": "W-FRI", "M": "ME", "Q": "QE", "A": "YE"}.get(freq, "ME")
    marks = pd.Series(1, index=index).resample(rule).last().index
    dates = []
    for m in marks:
        pos = index.searchsorted(m, side="right") - 1
        if pos >= 0:
            dates.append(index[pos])
    return sorted(set(dates))


def _target_weights(strategy, hist: pd.DataFrame, current: pd.Series, cons: opt.Constraints,
                    cov_method: str, rf: float, vol_target: float, live: pd.Series | None = None) -> pd.Series:
    tickers = list(hist.columns)
    if callable(strategy):
        # custom rules see what the book actually holds right now (drifted weights), not the starting weights
        return strategy(hist, live if live is not None else current).reindex(tickers).fillna(0.0)
    if strategy in ("current", "buy_hold"):
        return current.reindex(tickers).fillna(0.0)
    cov = estimate_cov(hist, cov_method) * TRADING_DAYS
    if strategy == "vol_target":
        scaled = opt.vol_target_scale(current.reindex(tickers).fillna(0.0), cov, vol_target)
        return scaled["scaled_weights"]
    if strategy == "equal_weight":
        return pd.Series(1.0 / len(tickers), index=tickers)
    if strategy == "inverse_vol":
        return opt.inverse_vol(cov).weights
    if strategy == "min_variance":
        r = opt.min_variance(cov, cons)
    elif strategy == "risk_parity":
        r = opt.risk_parity(cov, cons)
    elif strategy == "max_diversification":
        r = opt.max_diversification(cov, cons)
    elif strategy == "max_sharpe":
        mu = opt.expected_returns(hist, "shrunk")
        r = opt.max_sharpe(mu, cov, cons, rf)
    else:
        raise ValueError(f"Unknown strategy {strategy}")
    if not r.ok:
        return pd.Series(1.0 / len(tickers), index=tickers)
    return r.weights.fillna(0.0)


def run_backtest(prices: pd.DataFrame, strategy, current_weights: pd.Series | None = None,
                 rebalance: str = "M", lookback: int = 252, cost_bps: float = 10.0,
                 cons: opt.Constraints | None = None, cov_method: str = "ledoit_wolf",
                 rf: float = 0.0, start: str | None = None, vol_target: float = 0.10,
                 benchmark: pd.Series | None = None) -> BacktestResult:
    px = prices.dropna(axis=1, how="all").sort_index()
    rets = simple_returns(px).fillna(0.0)
    if start:
        first_idx = max(rets.index.searchsorted(pd.Timestamp(start)), lookback)
    else:
        first_idx = lookback
    if first_idx >= len(rets) - 2:
        raise ValueError("Not enough history for the requested lookback.")
    cons = cons or opt.Constraints(long_only=True, w_max=0.25)
    tickers = list(px.columns)
    current = (current_weights.reindex(tickers).fillna(0.0) if current_weights is not None
               else pd.Series(1.0 / len(tickers), index=tickers))
    if strategy in ("current", "buy_hold", "vol_target") and current.abs().sum() == 0:
        current = pd.Series(1.0 / len(tickers), index=tickers)

    dates = rets.index[first_idx:]
    rb_dates = set(_rebalance_dates(dates, rebalance))
    w = np.zeros(len(tickers))
    port_ret = []
    turnover = []
    weights_hist = []
    cost = cost_bps / 1e4
    initialised = False
    for i, d in enumerate(dates):
        r = rets.loc[d].to_numpy(dtype=float)
        if not initialised or (d in rb_dates and strategy != "buy_hold"):
            hist = rets.iloc[max(0, rets.index.get_loc(d) - lookback): rets.index.get_loc(d)]
            live = pd.Series(w, index=tickers) if initialised else None
            target = _target_weights(strategy, hist, current, cons, cov_method, rf, vol_target, live).reindex(tickers).fillna(0.0).to_numpy()
            to = float(np.abs(target - w).sum()) if initialised else float(np.abs(target).sum())
            tc = to * cost
            w = target
            initialised = True
        else:
            to, tc = 0.0, 0.0
        pr = float(w @ r) - tc
        port_ret.append(pr)
        turnover.append(to)
        weights_hist.append(w.copy())
        # drift
        gross_growth = 1.0 + pr
        w = w * (1.0 + r) / gross_growth if gross_growth != 0 else w
    name = strategy if isinstance(strategy, str) else getattr(strategy, "__name__", "custom")
    returns = pd.Series(port_ret, index=dates, name=name)
    equity = (1.0 + returns).cumprod()
    wdf = pd.DataFrame(weights_hist, index=dates, columns=tickers)
    to_series = pd.Series(turnover, index=dates)
    stats = performance_stats(returns, rf=rf, benchmark=benchmark)
    stats["annual_turnover"] = float(to_series.sum() / (len(dates) / TRADING_DAYS))
    stats["avg_gross"] = float(wdf.abs().sum(axis=1).mean())
    return BacktestResult(name, returns, equity, wdf, to_series, stats,
                          {"rebalance": rebalance, "lookback": lookback, "cost_bps": cost_bps})


def compare(prices: pd.DataFrame, strategies, **kw) -> dict[str, BacktestResult]:
    """strategies: a list of built-in names, or a dict {label: name-or-callable}."""
    items = strategies.items() if isinstance(strategies, dict) else [(s, s) for s in strategies]
    out = {}
    for label, s in items:
        try:
            out[label] = run_backtest(prices, s, **kw)
        except Exception as exc:  # noqa: BLE001
            out[label] = exc
    return out
