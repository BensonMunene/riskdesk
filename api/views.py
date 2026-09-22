"""JSON API so the investment team can pull risk numbers into notebooks, Excel or other systems.

Authentication: Django session (browser) or HTTP Basic (scripts). See /api-docs/.
"""
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response

from analytics import optimize as opt
from analytics.covariance import estimate_cov
from analytics.returns import TRADING_DAYS, simple_returns, window
from core.models import Asset, Portfolio
from web.compare import compare, trade_table
from web.services import asset_meta_frame, build_analyzer, df_records, jsonable, limits_for, load_market_data, series_xy


def _analyzer(pk):
    portfolio = get_object_or_404(Portfolio, pk=pk)
    return portfolio, build_analyzer(portfolio)


@api_view(["GET"])
def api_root(request):
    return Response({
        "portfolios": "/api/portfolios/",
        "portfolio": "/api/portfolios/<id>/  (summary)",
        "sub_resources": ["positions", "risk", "factors", "var", "stress", "limits", "insights", "liquidity", "correlation"],
        "whatif": "POST /api/portfolios/<id>/whatif/  {\"trades\": {\"AAPL\": 1000, \"SPY\": -500}}",
        "optimize": "POST /api/portfolios/<id>/optimize/  {\"method\": \"max_sharpe\", \"w_max\": 0.1, \"long_only\": true}",
        "assets": "/api/assets/",
        "prices": "/api/prices/<ticker>/?days=252",
    })


@api_view(["GET"])
def portfolio_list(request):
    out = []
    for p in Portfolio.objects.all():
        a = build_analyzer(p)
        s = a.summary() if len(a.positions) else {"nav": p.cash, "n_positions": 0}
        out.append({"id": p.id, "name": p.name, "benchmark": p.benchmark, **jsonable(s)})
    return Response(out)


@api_view(["GET"])
def portfolio_summary(request, pk):
    p, a = _analyzer(pk)
    return Response({"id": p.id, "name": p.name, "description": p.description, "settings": a.settings,
                     "summary": jsonable(a.summary()), "performance": jsonable(a.performance),
                     "concentration": jsonable(a.concentration)})


@api_view(["GET"])
def portfolio_positions(request, pk):
    p, a = _analyzer(pk)
    df = a.positions.copy()
    df["price"] = a.last_prices.reindex(df.index)
    df["market_value"] = a.market_values
    df["weight"] = a.weights
    return Response({"as_of": jsonable(a.as_of), "nav": a.nav, "cash": a.cash, "positions": df_records(df)})


@api_view(["GET"])
def portfolio_risk(request, pk):
    p, a = _analyzer(pk)
    return Response({"portfolio_vol": a.portfolio_vol, "positions": df_records(a.rc),
                     "sectors": df_records(a.sector_rc, "sector"), "asset_classes": df_records(a.asset_class_rc, "asset_class")})


@api_view(["GET"])
def portfolio_factors(request, pk):
    p, a = _analyzer(pk)
    fd = a.factor_decomposition
    return Response({"exposures": df_records(fd["exposures"], "factor"), "systematic_vol": fd["systematic_vol"],
                     "idiosyncratic_vol": fd["idiosyncratic_vol"], "systematic_share": jsonable(fd["systematic_share"]),
                     "asset_betas": df_records(a.factor_model["betas"]), "r2": jsonable(a.factor_model["r2"]),
                     "n_obs": a.factor_model["n_obs"]})


@api_view(["GET"])
def portfolio_var(request, pk):
    p, a = _analyzer(pk)
    return Response({"nav": a.nav, "var": [jsonable(v.as_dict()) for v in a.var_table],
                     "contributions": df_records(a.var_contrib), "drawdowns": df_records(a.drawdowns, "episode"),
                     "worst_windows": df_records(a.worst_windows, "rank")})


@api_view(["GET"])
def portfolio_stress(request, pk):
    p, a = _analyzer(pk)
    def strip(d):
        return {k: jsonable(v) for k, v in d.items() if k not in ("asset_returns", "position_pnl", "beta_filled")}
    return Response({"historical": [strip(d) for d in a.stress_historical],
                     "hypothetical": [strip(d) for d in a.stress_hypothetical],
                     "single_name": df_records(a.single_name_shocks)})


@api_view(["GET"])
def portfolio_limits(request, pk):
    p, a = _analyzer(pk)
    return Response(jsonable(a.check_limits(limits_for(p))))


@api_view(["GET"])
def portfolio_insights(request, pk):
    p, a = _analyzer(pk)
    return Response(jsonable([i.as_dict() for i in a.insights(a.check_limits(limits_for(p)))]))


@api_view(["GET"])
def portfolio_liquidity(request, pk):
    p, a = _analyzer(pk)
    return Response({"positions": df_records(a.liquidity), "profile": df_records(a.liquidity_profile, "bucket")})


@api_view(["GET"])
def portfolio_correlation(request, pk):
    p, a = _analyzer(pk)
    return Response({"tickers": list(a.corr.index), "matrix": a.corr.round(4).values.tolist(), "clusters": jsonable(a.clusters)})


@api_view(["POST"])
def portfolio_whatif(request, pk):
    p, a = _analyzer(pk)
    trades = {str(k).upper(): float(v) for k, v in (request.data.get("trades") or {}).items() if float(v) != 0}
    if not trades:
        return Response({"error": "Provide trades as {ticker: share_delta}"}, status=400)
    try:
        b = a.with_trades(trades, asset_meta_frame())
    except ValueError as exc:
        return Response({"error": str(exc)}, status=400)
    limits = limits_for(p)
    return Response({"trades": trade_table(a, trades), "comparison": compare(a, b, limits),
                     "after": {"summary": jsonable(b.summary()), "risk": df_records(b.rc),
                               "insights": jsonable([i.as_dict() for i in b.insights(b.check_limits(limits))])}})


@api_view(["POST"])
def portfolio_optimize(request, pk):
    p, a = _analyzer(pk)
    d = request.data
    method = d.get("method", "max_sharpe")
    tickers = [t.upper() for t in d.get("tickers", list(a.positions.index)) if t.upper() in a.prices.columns]
    rets = window(simple_returns(a.prices[tickers]), a.settings["lookback_days"])
    cov = estimate_cov(rets, a.settings["cov_method"]) * TRADING_DAYS
    rf = a.settings["risk_free_rate"]
    mu = opt.expected_returns(rets, d.get("return_model", "shrunk"), market=a.benchmark_returns, rf=rf)
    cons = opt.Constraints(long_only=bool(d.get("long_only", True)), budget=d.get("budget", 1.0),
                           w_min=float(d.get("w_min", 0.0)), w_max=float(d.get("w_max", 0.25)),
                           gross_max=d.get("gross_max"), turnover_max=d.get("turnover_max"),
                           w0=a.weights.reindex(tickers).fillna(0.0), max_vol=d.get("max_vol"))
    try:
        res = opt.run(method, mu, cov, cons, rf=rf, risk_aversion=float(d.get("risk_aversion", 5.0)),
                      target=float(d.get("target", 0.10)), views=d.get("views", []),
                      market_weights=a.weights.abs().reindex(tickers).fillna(0.0))
    except Exception as exc:  # noqa: BLE001
        return Response({"error": str(exc)}, status=400)
    if not res.ok:
        return Response({"error": res.message, "status": res.status}, status=422)
    return Response({"method": res.method, "status": res.status, "weights": jsonable(res.weights.round(6)),
                     "expected_return": res.expected_return, "vol": res.vol, "sharpe": res.sharpe,
                     "trades": df_records(a.weights_to_shares(res.weights)), "constraints": cons.describe()})


@api_view(["GET"])
def asset_list(request):
    return Response(list(Asset.objects.values("ticker", "name", "asset_class", "sector", "industry", "is_factor_etf")))


@api_view(["GET"])
def prices(request, ticker):
    close, volume = load_market_data()
    t = ticker.upper()
    if t not in close.columns:
        return Response({"error": f"{t} not loaded"}, status=404)
    days = int(request.GET.get("days", 252))
    return Response({"ticker": t, "close": series_xy(close[t].dropna().iloc[-days:]),
                     "volume": series_xy(volume[t].iloc[-days:]) if t in volume.columns else None})
