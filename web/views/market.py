import numpy as np
import pandas as pd
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from analytics.factors import build_factor_returns, factor_regression, required_tickers
from analytics.returns import TRADING_DAYS, simple_returns
from core.models import Asset, Position
from web.services import jsonable, load_market_data, series_xy


@login_required
def universe(request):
    close, volume = load_market_data()
    assets = {a.ticker: a for a in Asset.objects.all()}
    rows = []
    if not close.empty:
        rets = simple_returns(close)
        last = close.ffill().iloc[-1]
        ytd_start = close.index[close.index >= pd.Timestamp(close.index[-1].year, 1, 1)]
        for t in close.columns:
            a = assets.get(t)
            s = close[t].dropna()
            if s.empty:
                continue
            r = rets[t].dropna()
            rows.append({
                "ticker": t, "name": a.name if a else t, "sector": (a.sector if a else "") or "-",
                "asset_class": a.asset_class if a else "", "is_factor": a.is_factor_etf if a else False,
                "price": float(last[t]),
                "ret_1d": float(r.iloc[-1]) if len(r) else np.nan,
                "ret_1m": float((1 + r.iloc[-21:]).prod() - 1) if len(r) >= 21 else np.nan,
                "ret_ytd": float(s.iloc[-1] / s.loc[ytd_start[0]] - 1) if len(ytd_start) else np.nan,
                "vol_1y": float(r.iloc[-252:].std() * np.sqrt(TRADING_DAYS)) if len(r) >= 60 else np.nan,
                "adv_dollar": float((volume[t].tail(30) * close[t].tail(30)).mean()) if t in volume.columns else np.nan,
                "first": s.index[0].date(), "last": s.index[-1].date(), "n": int(len(s)),
            })
    rows.sort(key=lambda r: (r["asset_class"] != "Equity", r["ticker"]))
    return render(request, "web/market.html", {"rows": rows, "n": len(rows),
                                               "factor_tickers": required_tickers()})


@login_required
def asset_detail(request, ticker):
    asset = get_object_or_404(Asset, ticker=ticker.upper())
    close, volume = load_market_data()
    t = asset.ticker
    s = close[t].dropna()
    r = simple_returns(s)
    etfs = [x for x in required_tickers() if x in close.columns]
    fr = build_factor_returns(simple_returns(close[etfs])).iloc[-504:]
    reg = factor_regression(r.to_frame(t).iloc[-504:], fr)
    betas = reg["betas"].loc[t]
    holders = Position.objects.filter(asset=asset).select_related("portfolio")
    stats = {
        "price": float(s.iloc[-1]), "ret_1m": float((1 + r.iloc[-21:]).prod() - 1), "ret_1y": float((1 + r.iloc[-252:]).prod() - 1),
        "vol_1y": float(r.iloc[-252:].std() * np.sqrt(TRADING_DAYS)), "beta": float(betas["MKT"]),
        "r2": float(reg["r2"][t]), "resid_vol": float(reg["resid_vol"][t]),
        "adv_dollar": float((volume[t].tail(30) * close[t].tail(30)).mean()) if t in volume.columns else np.nan,
        "max_dd_1y": float(((1 + r.iloc[-252:]).cumprod() / (1 + r.iloc[-252:]).cumprod().cummax() - 1).min()),
        "worst_day": float(r.min()), "worst_day_date": r.idxmin().date(),
    }
    charts = jsonable({"price": series_xy(s.iloc[-756:]), "betas": {"labels": list(betas.index), "values": list(betas.values)},
                       "roll_vol": series_xy((r.rolling(21).std() * np.sqrt(TRADING_DAYS)).dropna().iloc[-504:])})
    return render(request, "web/asset_detail.html", {"asset": asset, "stats": stats, "betas": betas, "tstats": reg["tstats"].loc[t],
                                                     "holders": holders, "charts": charts})
