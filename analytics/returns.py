"""Return construction and annualisation helpers."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def simple_returns(prices):
    """Daily simple returns from a price matrix (rows = dates)."""
    return prices.sort_index().pct_change(fill_method=None).iloc[1:]


def log_returns(prices):
    return np.log(prices.sort_index()).diff().iloc[1:]


def annualize_return(daily: pd.Series, periods: int = TRADING_DAYS) -> float:
    """Geometric annualised return."""
    daily = daily.dropna()
    if len(daily) == 0:
        return float("nan")
    growth = float((1.0 + daily).prod())
    if growth <= 0:
        return -1.0
    return growth ** (periods / len(daily)) - 1.0


def annualize_vol(daily: pd.Series, periods: int = TRADING_DAYS) -> float:
    return float(daily.dropna().std(ddof=1)) * np.sqrt(periods)


def cumulative(daily: pd.Series) -> pd.Series:
    return (1.0 + daily.fillna(0.0)).cumprod() - 1.0


def window(returns: pd.DataFrame, lookback_days):
    """Last `lookback_days` rows (or all if None / 0)."""
    if lookback_days is None or lookback_days <= 0 or lookback_days >= len(returns):
        return returns
    return returns.iloc[-lookback_days:]
