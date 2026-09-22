"""Stress testing: historical replay and hypothetical factor shocks."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .factors import factor_implied_returns

# Built-in historical windows (start, end inclusive). Assets without history in the
# window get a beta-implied return from the factor model so newer positions are
# still stressed rather than silently ignored.
HISTORICAL_SCENARIOS = [
    {"key": "covid_crash", "name": "COVID crash", "start": "2020-02-19", "end": "2020-03-23",
     "description": "Global pandemic sell-off; S&P 500 fell ~34% peak to trough."},
    {"key": "covid_rebound", "name": "COVID rebound", "start": "2020-03-23", "end": "2020-06-08",
     "description": "V-shaped recovery on fiscal and monetary stimulus."},
    {"key": "rates_2022_h1", "name": "2022 rate shock (H1)", "start": "2022-01-03", "end": "2022-06-16",
     "description": "Fed hiking cycle begins; growth and duration sold off together."},
    {"key": "bear_2022", "name": "2022 bear market", "start": "2022-01-03", "end": "2022-10-12",
     "description": "Peak-to-trough of the 2022 equity bear market."},
    {"key": "svb_2023", "name": "SVB banking stress", "start": "2023-03-08", "end": "2023-03-13",
     "description": "Regional bank run; financials and rates moved sharply."},
    {"key": "yen_carry_2024", "name": "Aug 2024 vol spike", "start": "2024-07-16", "end": "2024-08-05",
     "description": "Yen carry-trade unwind; VIX briefly above 60."},
    {"key": "deepseek_2025", "name": "DeepSeek AI sell-off", "start": "2025-01-24", "end": "2025-01-27",
     "description": "One-day rotation out of AI infrastructure names."},
    {"key": "tariff_2025", "name": "Apr 2025 tariff shock", "start": "2025-04-02", "end": "2025-04-08",
     "description": "Reciprocal tariff announcement; broad risk-off and dollar weakness."},
]

# Hypothetical factor shocks expressed as factor returns over the scenario horizon.
HYPOTHETICAL_SCENARIOS = [
    {"key": "equity_down_10", "name": "Equities -10%", "shocks": {"MKT": -0.10},
     "description": "Broad equity drawdown, other factors unchanged."},
    {"key": "equity_down_20_credit", "name": "Equities -20%, credit -8%",
     "shocks": {"MKT": -0.20, "CREDIT": -0.08, "SIZE": -0.05},
     "description": "Recessionary sell-off with credit spreads widening and small caps lagging."},
    {"key": "rates_up", "name": "Rates +100bp (TLT -12%)", "shocks": {"RATES": -0.12, "MKT": -0.04, "LOWVOL": 0.02},
     "description": "Hawkish repricing; long duration sells off, equities soft."},
    {"key": "rates_down", "name": "Flight to quality (TLT +8%)", "shocks": {"RATES": 0.08, "MKT": -0.06, "GOLD": 0.05},
     "description": "Growth scare; bonds and gold rally, equities fall."},
    {"key": "oil_spike", "name": "Oil +25%", "shocks": {"OIL": 0.25, "MKT": -0.03, "VALUE": 0.03},
     "description": "Supply shock; energy outperforms, value beats growth."},
    {"key": "dollar_up", "name": "Dollar +5%", "shocks": {"USD": 0.05, "MKT": -0.02, "GOLD": -0.04},
     "description": "Dollar squeeze; commodities and multinationals under pressure."},
    {"key": "momentum_crash", "name": "Momentum crash", "shocks": {"MOM": -0.10, "VALUE": 0.08, "SIZE": 0.05},
     "description": "Sharp factor rotation out of winners into laggards (e.g. Nov 2020, Jan 2021)."},
    {"key": "stagflation", "name": "Stagflation", "shocks": {"MKT": -0.12, "RATES": -0.08, "OIL": 0.20, "GOLD": 0.08, "CREDIT": -0.05},
     "description": "Growth down, inflation up: equities and bonds both fall, commodities rally."},
]


def historical_replay(weights: pd.Series, prices: pd.DataFrame, start: str, end: str,
                      betas: pd.DataFrame | None = None, factor_prices: pd.DataFrame | None = None,
                      factor_spec: dict | None = None) -> dict:
    """Apply the cumulative asset returns from a historical window to current weights."""
    px = prices.sort_index()
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    seg = px.loc[(px.index >= s) & (px.index <= e)]
    if len(seg) < 2:
        return {"available": False, "reason": "window not covered by price history"}
    first = seg.iloc[0]
    last = seg.iloc[-1]
    asset_ret = (last / first - 1.0)
    filled = pd.Series(False, index=weights.index)
    # beta-fill assets missing in the window
    if betas is not None and factor_prices is not None and factor_spec is not None:
        fseg = factor_prices.loc[(factor_prices.index >= s) & (factor_prices.index <= e)]
        if len(fseg) >= 2:
            etf_ret = fseg.iloc[-1] / fseg.iloc[0] - 1.0
            shocks = {}
            for name, (long, short) in factor_spec.items():
                if long in etf_ret.index:
                    shocks[name] = float(etf_ret[long] - (etf_ret[short] if short and short in etf_ret.index else 0.0))
            implied = factor_implied_returns(betas, shocks)
            for t in weights.index:
                if t not in asset_ret.index or pd.isna(asset_ret.get(t)):
                    if t in implied.index:
                        asset_ret[t] = float(implied[t])
                        filled[t] = True
    asset_ret = asset_ret.reindex(weights.index).fillna(0.0)
    pnl = weights * asset_ret
    return {
        "available": True,
        "start": seg.index[0],
        "end": seg.index[-1],
        "days": int(len(seg) - 1),
        "asset_returns": asset_ret,
        "position_pnl": pnl,
        "total_pnl": float(pnl.sum()),
        "beta_filled": filled,
    }


def hypothetical_shock(weights: pd.Series, betas: pd.DataFrame, shocks: dict[str, float],
                       resid_vol: pd.Series | None = None) -> dict:
    """Portfolio P&L from factor shocks propagated through asset betas."""
    implied = factor_implied_returns(betas, shocks).reindex(weights.index).fillna(0.0)
    pnl = weights * implied
    return {
        "available": True,
        "asset_returns": implied,
        "position_pnl": pnl,
        "total_pnl": float(pnl.sum()),
        "shocks": shocks,
    }


def single_asset_shocks(weights: pd.Series, corr: pd.DataFrame, vols_daily: pd.Series,
                        shock_sigma: float = 3.0) -> pd.DataFrame:
    """What if each holding drops by `shock_sigma` daily standard deviations, with
    correlated moves in the rest of the book (conditional expectation)?"""
    rows = []
    for t in weights.index:
        if t not in corr.index:
            continue
        own = -shock_sigma * float(vols_daily[t])
        # conditional moves: E[r_j | r_t] = rho_tj * sigma_j / sigma_t * r_t
        cond = corr[t] * vols_daily / float(vols_daily[t]) * own
        cond = cond.reindex(weights.index).fillna(0.0)
        pnl_total = float((weights * cond).sum())
        pnl_own = float(weights[t] * own)
        rows.append({"ticker": t, "shock": own, "own_pnl": pnl_own, "total_pnl": pnl_total,
                     "spillover": pnl_total - pnl_own})
    return pd.DataFrame(rows).set_index("ticker").sort_values("total_pnl")


def worst_windows(port_returns: pd.Series, window_days: int = 10, top: int = 5) -> pd.DataFrame:
    """Data-driven worst rolling windows for the current book (pro forma)."""
    roll = (1.0 + port_returns).rolling(window_days).apply(np.prod, raw=True) - 1.0
    roll = roll.dropna().sort_values()
    rows = []
    used: list[pd.Timestamp] = []
    for end, r in roll.items():
        if any(abs((end - u).days) < window_days * 2 for u in used):
            continue
        start = port_returns.index[port_returns.index.get_loc(end) - window_days + 1]
        rows.append({"start": start, "end": end, "return": float(r)})
        used.append(end)
        if len(rows) >= top:
            break
    return pd.DataFrame(rows)
