"""Glue between the Django models and the analytics library."""
from __future__ import annotations

import datetime as dt
import json
import math
from collections import OrderedDict

import numpy as np
import pandas as pd
from django.db.models import Count, Max

from analytics.engine import PortfolioAnalyzer
from core.models import Asset, Portfolio, Price

_PRICE_CACHE: dict = {}
_ANALYZER_CACHE: "OrderedDict[tuple, PortfolioAnalyzer]" = OrderedDict()
_ANALYZER_CACHE_SIZE = 24


def _price_version() -> tuple:
    agg = Price.objects.aggregate(n=Count("id"), d=Max("date"))
    return (agg["n"], agg["d"])


def load_market_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Wide close and volume matrices for every asset, cached per process until prices change."""
    version = _price_version()
    cached = _PRICE_CACHE.get("v")
    if cached == version and "close" in _PRICE_CACHE:
        return _PRICE_CACHE["close"], _PRICE_CACHE["volume"]
    rows = Price.objects.values_list("asset__ticker", "date", "close", "volume")
    df = pd.DataFrame.from_records(list(rows), columns=["ticker", "date", "close", "volume"])
    if df.empty:
        close = volume = pd.DataFrame()
    else:
        df["date"] = pd.to_datetime(df["date"])
        close = df.pivot(index="date", columns="ticker", values="close").sort_index()
        volume = df.pivot(index="date", columns="ticker", values="volume").sort_index().fillna(0)
    _PRICE_CACHE.update({"v": version, "close": close, "volume": volume})
    return close, volume


def invalidate_market_cache():
    _PRICE_CACHE.clear()


def asset_meta_frame() -> pd.DataFrame:
    rows = Asset.objects.values("ticker", "name", "sector", "asset_class", "industry", "country", "market_cap")
    df = pd.DataFrame.from_records(list(rows))
    if df.empty:
        return pd.DataFrame(columns=["name", "sector", "asset_class", "industry", "country", "market_cap"])
    df["sector"] = df["sector"].replace("", "Unclassified")
    return df.set_index("ticker")


def positions_frame(portfolio: Portfolio) -> pd.DataFrame:
    qs = portfolio.positions.select_related("asset")
    rows = [{"ticker": p.asset.ticker, "quantity": p.quantity, "avg_cost": p.avg_cost,
             "name": p.asset.name, "sector": p.asset.sector or "Unclassified",
             "asset_class": p.asset.asset_class, "industry": p.asset.industry, "country": p.asset.country}
            for p in qs]
    if not rows:
        return pd.DataFrame(columns=["quantity", "avg_cost", "name", "sector", "asset_class", "industry", "country"])
    return pd.DataFrame(rows).set_index("ticker")


def _positions_version(portfolio: Portfolio) -> tuple:
    agg = portfolio.positions.aggregate(n=Count("id"), u=Max("updated_at"))
    return (agg["n"], agg["u"])


def build_analyzer(portfolio: Portfolio, as_of: pd.Timestamp | dt.date | None = None,
                   positions: pd.DataFrame | None = None, cash: float | None = None) -> PortfolioAnalyzer:
    """Build (or fetch from a small in-process cache) the analyzer for a portfolio.

    The cache key covers everything the result depends on: positions, cash/settings
    (portfolio.updated_at), the price table version and the as-of date."""
    cacheable = positions is None and cash is None
    key = None
    if cacheable:
        key = (portfolio.id, portfolio.updated_at, _positions_version(portfolio), _price_version(),
               str(pd.Timestamp(as_of).date()) if as_of is not None else None,
               json.dumps(portfolio.analytics_settings(), sort_keys=True, default=str))
        hit = _ANALYZER_CACHE.get(key)
        if hit is not None:
            _ANALYZER_CACHE.move_to_end(key)
            return hit
    close, volume = load_market_data()
    if as_of is not None:
        as_of = pd.Timestamp(as_of)
        close = close.loc[:as_of]
        volume = volume.loc[:as_of]
    pos = positions_frame(portfolio) if positions is None else positions
    a = PortfolioAnalyzer(close, volume, pos, portfolio.cash if cash is None else cash,
                          portfolio.analytics_settings(), portfolio.name)
    if cacheable:
        _ANALYZER_CACHE[key] = a
        while len(_ANALYZER_CACHE) > _ANALYZER_CACHE_SIZE:
            _ANALYZER_CACHE.popitem(last=False)
    return a


def parse_as_of(raw: str | None):
    """Validate an ?as_of=YYYY-MM-DD query value against the loaded price history."""
    if not raw:
        return None, None
    try:
        d = pd.Timestamp(raw)
    except (ValueError, TypeError):
        return None, f"Ignored invalid as-of date '{raw}'."
    close, _ = load_market_data()
    if close.empty:
        return None, None
    first, last = close.index[0], close.index[-1]
    if d >= last:
        return None, None  # latest data: no need to truncate
    if d < first + pd.Timedelta(days=90):
        return None, f"As-of date {d.date()} is too early for the loaded history (starts {first.date()})."
    return d, None


def limits_for(portfolio: Portfolio) -> list[dict]:
    return [lim.as_dict() for lim in portfolio.limits.filter(is_active=True)]


# ----------------------------------------------------------------------------- JSON helpers

def jsonable(obj):
    """Recursively convert numpy / pandas objects into JSON-serialisable Python."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, dt.datetime, dt.date)):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, pd.Series):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, pd.DataFrame):
        return df_records(obj)
    if isinstance(obj, pd.Index):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [jsonable(v) for v in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if hasattr(obj, "as_dict"):
        return jsonable(obj.as_dict())
    if hasattr(obj, "__dict__"):
        return jsonable(vars(obj))
    return str(obj)


def df_records(df: pd.DataFrame, index_name: str = "ticker") -> list[dict]:
    if df is None or len(df) == 0:
        return []
    d = df.copy()
    d.index.name = d.index.name or index_name
    d = d.reset_index()
    return [jsonable(r) for r in d.to_dict(orient="records")]


def series_xy(s: pd.Series) -> dict:
    s = s.dropna()
    return {"x": [d.strftime("%Y-%m-%d") for d in s.index], "y": [jsonable(float(v)) for v in s.values]}


def limit_status_summary(results: list[dict]) -> dict:
    counts = {"breach": 0, "warn": 0, "ok": 0, "n/a": 0}
    for r in results:
        counts[r.get("status", "n/a")] = counts.get(r.get("status", "n/a"), 0) + 1
    counts["total"] = len(results)
    counts["overall"] = "breach" if counts["breach"] else ("warn" if counts["warn"] else "ok")
    return counts


def parse_trade_lines(raw_lines: list[dict], analyzer: PortfolioAnalyzer) -> tuple[dict, list[str]]:
    """Convert form rows into {ticker: share_delta}. Rows may specify shares, a target
    weight, or a weight delta; weights are converted at the last price."""
    trades: dict[str, float] = {}
    errors: list[str] = []
    for row in raw_lines:
        t = (row.get("ticker") or "").strip().upper()
        if not t:
            continue
        if t not in analyzer.prices.columns:
            errors.append(f"{t}: no price history loaded (add it with fetch_prices).")
            continue
        px = float(analyzer.last_prices[t])
        mode = row.get("mode", "shares")
        try:
            val = float(row.get("value", 0) or 0)
        except ValueError:
            errors.append(f"{t}: invalid number")
            continue
        if mode == "shares":
            dq = val
        elif mode == "target_weight":
            target_q = val * analyzer.nav / px
            dq = target_q - float(analyzer.positions["quantity"].get(t, 0.0))
        elif mode == "delta_weight":
            dq = val * analyzer.nav / px
        elif mode == "dollars":
            dq = val / px
        else:
            errors.append(f"{t}: unknown mode {mode}")
            continue
        dq = float(np.round(dq))
        if dq != 0:
            trades[t] = trades.get(t, 0.0) + dq
    return trades, errors
