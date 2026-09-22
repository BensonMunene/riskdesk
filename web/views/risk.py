import numpy as np
import pandas as pd
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from analytics.factors import FACTOR_DESCRIPTIONS, FACTOR_SPEC
from core.models import RiskSnapshot
from web.forms import ShockForm
from web.services import build_analyzer, jsonable, series_xy

from .common import chart, portfolio_context


@login_required
def decomposition(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    rc = a.rc
    charts = chart(
        contrib={"labels": list(rc.index), "pct": list(rc["pct"]), "weight": list(rc["weight"]),
                 "sector": list(rc["sector"])},
        bubble={"labels": list(rc.index), "weight": list(rc["weight"].abs()), "risk": list(rc["pct"]),
                "vol": list(rc["standalone_vol"]), "sector": list(rc["sector"]), "mv": list(rc["market_value"].abs())},
        sector={"labels": list(a.sector_rc.index), "risk": list(a.sector_rc["pct"]), "gross": list(a.sector_rc["gross"])},
        asset_class={"labels": list(a.asset_class_rc.index), "risk": list(a.asset_class_rc["pct"]),
                     "gross": list(a.asset_class_rc["gross"])},
    )
    ctx.update({"rc": rc, "sector_rc": a.sector_rc, "asset_class_rc": a.asset_class_rc, "charts": charts,
                "var_contrib": a.var_contrib.head(15)})
    return render(request, "web/risk_decomposition.html", ctx)


@login_required
def factors(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    fm = a.factor_model
    fd = a.factor_decomposition
    betas = fm["betas"].join(a.positions[["sector"]]).join(a.weights.rename("weight"))
    betas["r2"] = fm["r2"]
    betas["resid_vol"] = fm["resid_vol"]
    betas = betas.sort_values("weight", key=np.abs, ascending=False)
    # portfolio-level t-stats approximated by weighted average of asset t-stats
    roll = a.rolling()
    charts = chart(
        exposures={"labels": list(fd["exposures"].index), "beta": list(fd["exposures"]["beta"]),
                   "share": list(fd["exposures"]["pct_of_total_var"]), "vol_contrib": list(fd["exposures"]["vol_contrib"])},
        heatmap={"x": list(fm["betas"].columns), "y": list(betas.index),
                 "z": [[float(betas.loc[t, f]) for f in fm["betas"].columns] for t in betas.index]},
        rolling_beta=series_xy(roll["beta"]) if "beta" in roll else None,
        split={"systematic": fd["systematic_vol"], "idio": fd["idiosyncratic_vol"], "total": fd["total_vol"]},
        factor_corr={"labels": list(fd["factor_cov"].index),
                     "z": (fd["factor_cov"] / np.sqrt(np.outer(np.diag(fd["factor_cov"]), np.diag(fd["factor_cov"])))).round(3).values.tolist()},
    )
    ctx.update({"fd": fd, "exposures": fd["exposures"], "betas": betas, "factor_desc": FACTOR_DESCRIPTIONS,
                "factor_spec": FACTOR_SPEC, "n_obs": fm["n_obs"], "charts": charts,
                "idio_share": 1.0 - fd["systematic_share"]})
    return render(request, "web/risk_factors.html", ctx)


@login_required
def tail(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    pr = a.port_returns_window
    roll = a.rolling()
    var_rows = [v.as_dict() for v in a.var_table]
    hist_counts, hist_edges = np.histogram(pr.values, bins=60)
    charts = chart(
        hist={"counts": hist_counts.tolist(), "edges": hist_edges.tolist(),
              "var95": -next(v.var for v in a.var_table if v.method == "historical" and v.confidence == 0.95),
              "var99": -next(v.var for v in a.var_table if v.method == "historical" and v.confidence == 0.99),
              "cvar99": -next(v.cvar for v in a.var_table if v.method == "historical" and v.confidence == 0.99)},
        drawdown=series_xy(roll["drawdown"]),
        rolling_vol={"port": series_xy(roll["vol"]), "bench": series_xy(roll["bench_vol"]) if "bench_vol" in roll else None},
        cum=series_xy((1 + a.port_returns).cumprod() - 1),
        var_contrib={"labels": list(a.var_contrib.index[:15]), "values": list(a.var_contrib["cvar_contrib"][:15])},
    )
    ctx.update({"var_rows": var_rows, "perf": a.performance, "drawdowns": a.drawdowns, "worst_windows": a.worst_windows,
                "var_contrib": a.var_contrib, "charts": charts, "n_days": len(pr)})
    return render(request, "web/risk_tail.html", ctx)


@login_required
def stress(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    custom = None
    form = ShockForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        shocks = {k: v for k, v in form.cleaned_data.items() if v}
        if shocks:
            custom = a.custom_shock(shocks)
            custom["detail"] = _scenario_detail(a, custom)
    hist = a.stress_historical
    hypo = a.stress_hypothetical
    selected = request.GET.get("scenario")
    detail = None
    for sc in hist + hypo:
        if sc["key"] == selected and sc.get("available"):
            detail = {"scenario": sc, "rows": _scenario_detail(a, sc)}
    charts = chart(
        hist={"labels": [s["name"] for s in hist if s.get("available")],
              "pnl": [s["total_pnl"] for s in hist if s.get("available")],
              "bench": [s.get("benchmark_return") for s in hist if s.get("available")]},
        hypo={"labels": [s["name"] for s in hypo], "pnl": [s["total_pnl"] for s in hypo]},
        single={"labels": list(a.single_name_shocks.index[:15]), "own": list(a.single_name_shocks["own_pnl"][:15]),
                "spill": list(a.single_name_shocks["spillover"][:15])},
    )
    ctx.update({"hist": hist, "hypo": hypo, "form": form, "custom": custom, "detail": detail,
                "single": a.single_name_shocks, "worst_windows": a.worst_windows, "charts": charts,
                "factor_desc": FACTOR_DESCRIPTIONS})
    return render(request, "web/risk_stress.html", ctx)


def _scenario_detail(a, sc):
    pnl = sc["position_pnl"].sort_values()
    rows = []
    for t, v in pnl.items():
        rows.append({"ticker": t, "weight": float(a.weights.get(t, 0.0)), "asset_return": float(sc["asset_returns"].get(t, 0.0)),
                     "pnl": float(v), "pnl_dollar": float(v) * a.nav,
                     "beta_filled": bool(sc.get("beta_filled", pd.Series(dtype=bool)).get(t, False)),
                     "sector": str(a.positions["sector"].get(t, ""))})
    return rows


@login_required
def liquidity(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    liq = a.liquidity
    prof = a.liquidity_profile
    # pick a readable unit for the days-to-liquidate chart: minutes / hours / days (6.5h trading day)
    unit, scale = "days", 1.0
    if len(liq):
        mx = float(liq["days_to_liquidate"].replace(np.inf, 60.0).max())
        if mx < 1 / 6.5:
            unit, scale = "minutes", 6.5 * 60
        elif mx < 1:
            unit, scale = "hours", 6.5
    charts = chart(
        profile={"labels": list(prof.index), "pct": list(prof["pct_gross"]), "cum": list(prof["cumulative_pct"])} if len(prof) else None,
        days={"labels": list(liq.index), "days": [min(float(d), 60.0) * scale for d in liq["days_to_liquidate"]],
              "weight": list(liq["weight"].abs()), "unit": unit} if len(liq) else None,
        sector={"labels": list(a.sector_weights.index), "long": list(a.sector_weights["long"]),
                "short": list(a.sector_weights["short"])},
        asset_class={"labels": list(a.asset_class_weights.index), "gross": list(a.asset_class_weights["gross"])},
        top={"labels": list(a.weights.abs().sort_values(ascending=False).index[:15]),
             "values": list(a.weights.abs().sort_values(ascending=False)[:15])},
    )
    ctx.update({"liq": liq, "profile": prof, "concentration": a.concentration, "sector_weights": a.sector_weights,
                "asset_class_weights": a.asset_class_weights, "charts": charts,
                "participation": a.settings["liquidity_participation"]})
    return render(request, "web/risk_liquidity.html", ctx)


@login_required
def correlation(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    corr = a.corr
    order = list(a.rc.index)  # sort by risk contribution
    corr = corr.loc[order, order]
    vals, vecs = np.linalg.eigh(a.cov.to_numpy())
    vals = vals[::-1]
    share = vals / vals.sum()
    n = len(corr)
    mask = ~np.eye(n, dtype=bool)
    avg_corr = float(corr.to_numpy()[mask].mean()) if n > 1 else float("nan")
    w = a.weights.reindex(order)
    long_idx = [t for t in order if w[t] > 0]
    short_idx = [t for t in order if w[t] < 0]
    ls_corr = float(corr.loc[long_idx, short_idx].to_numpy().mean()) if long_idx and short_idx else float("nan")
    charts = chart(
        heatmap={"labels": order, "z": corr.round(2).values.tolist()},
        eigen={"labels": [f"PC{i + 1}" for i in range(min(10, n))], "share": share[:10].tolist(),
               "cum": np.cumsum(share)[:10].tolist()},
    )
    ctx.update({"clusters": a.clusters, "avg_corr": avg_corr, "ls_corr": ls_corr, "charts": charts,
                "pc1_share": float(share[0]) if n else float("nan"), "enb": a.concentration["enb"]})
    return render(request, "web/risk_correlation.html", ctx)


@login_required
def history(request, pk):
    ctx = portfolio_context(pk, request)
    portfolio = ctx["portfolio"]
    if request.method == "POST" and not ctx["empty"]:
        n = int(request.POST.get("days", 60))
        close = ctx["a"].prices
        dates = list(close.index[-n:])
        for d in dates:
            an = build_analyzer(portfolio, as_of=d)
            if len(an.positions) == 0:
                continue
            s = an.summary()
            metrics = {k: s[k] for k in ("nav", "gross_exposure", "net_exposure", "vol", "beta", "var", "cvar",
                                          "var_dollar", "n_positions", "current_drawdown")}
            metrics["max_risk_share"] = float(an.rc["pct"].max()) if len(an.rc) else None
            metrics["top5"] = an.concentration["top5"]
            RiskSnapshot.objects.update_or_create(portfolio=portfolio, as_of=d.date(), defaults={"metrics": jsonable(metrics)})
        messages.success(request, f"Computed {len(dates)} daily snapshots (current holdings held constant).")
        return redirect("risk_history", pk=pk)
    snaps = list(portfolio.snapshots.order_by("as_of"))
    series = {}
    if snaps:
        for key in ("vol", "var", "beta", "gross_exposure", "net_exposure", "max_risk_share", "top5", "current_drawdown", "var_dollar"):
            series[key] = {"x": [s.as_of.isoformat() for s in snaps],
                           "y": [s.metrics.get(key) for s in snaps]}
    ctx.update({"snapshots": snaps[-30:][::-1], "charts": jsonable(series), "n_snaps": len(snaps)})
    return render(request, "web/risk_history.html", ctx)
