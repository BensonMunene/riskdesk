"""Liquidity analytics based on average daily volume."""
from __future__ import annotations

import numpy as np
import pandas as pd

BUCKETS = [(1, "< 1 day"), (3, "1-3 days"), (10, "3-10 days"), (np.inf, "> 10 days")]


def average_daily_volume(volumes: pd.DataFrame, window: int = 30) -> pd.Series:
    return volumes.tail(window).replace(0, np.nan).mean().fillna(0.0)


def liquidity_table(shares: pd.Series, adv_shares: pd.Series, prices: pd.Series,
                    nav: float, participation: float = 0.20) -> pd.DataFrame:
    """Days to liquidate each position at a given participation rate of ADV."""
    idx = shares.index
    adv = adv_shares.reindex(idx).fillna(0.0)
    px = prices.reindex(idx)
    mv = shares * px
    with np.errstate(divide="ignore", invalid="ignore"):
        days = np.where(adv > 0, shares.abs() / (adv * participation), np.inf)
    adv_dollar = adv * px
    pct_adv = np.where(adv > 0, shares.abs() / adv, np.inf)
    df = pd.DataFrame({
        "shares": shares,
        "market_value": mv,
        "weight": mv / nav if nav else np.nan,
        "adv_shares": adv,
        "adv_dollar": adv_dollar,
        "pct_of_adv": pct_adv,
        "days_to_liquidate": days,
    }, index=idx)
    df["bucket"] = [bucket_label(d) for d in df["days_to_liquidate"]]
    return df.sort_values("days_to_liquidate", ascending=False)


def bucket_label(days: float) -> str:
    for limit, label in BUCKETS:
        if days < limit:
            return label
    return BUCKETS[-1][1]


def liquidity_profile(table: pd.DataFrame) -> pd.DataFrame:
    """Share of gross exposure that can be liquidated within each bucket."""
    gross = table["market_value"].abs()
    total = gross.sum()
    order = [b[1] for b in BUCKETS]
    prof = gross.groupby(table["bucket"]).sum().reindex(order).fillna(0.0)
    out = pd.DataFrame({"gross_value": prof, "pct_gross": prof / total if total else 0.0})
    out["cumulative_pct"] = out["pct_gross"].cumsum()
    return out


def weighted_days_to_liquidate(table: pd.DataFrame) -> float:
    gross = table["market_value"].abs()
    total = gross.sum()
    if total == 0:
        return float("nan")
    d = table["days_to_liquidate"].replace(np.inf, 250.0)
    return float((d * gross).sum() / total)


def liquidation_cost_estimate(table: pd.DataFrame, daily_vol: pd.Series, horizon_days: int = 1) -> pd.Series:
    """Rough market-impact estimate (square-root law): cost ~ sigma_daily * sqrt(shares / ADV).

    Returns estimated cost in dollars per position for liquidating over `horizon_days`.
    """
    sigma = daily_vol.reindex(table.index).fillna(daily_vol.median())
    with np.errstate(divide="ignore", invalid="ignore"):
        participation = np.where(table["adv_shares"] > 0,
                                 table["shares"].abs() / (table["adv_shares"] * horizon_days), 1.0)
    impact = 0.5 * sigma * np.sqrt(np.clip(participation, 0, 5))
    return impact * table["market_value"].abs()
