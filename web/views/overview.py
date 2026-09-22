from django.contrib.auth.decorators import login_required
from django.db.models import Max, Min
from django.shortcuts import render

from core.models import Portfolio, Price
from web.services import build_analyzer, limit_status_summary, limits_for, series_xy

from .common import chart, portfolio_context


@login_required
def home(request):
    cards = []
    for p in Portfolio.objects.all():
        a = build_analyzer(p)
        if len(a.positions) == 0:
            cards.append({"portfolio": p, "empty": True})
            continue
        s = a.summary()
        lim = a.check_limits(limits_for(p))
        cards.append({"portfolio": p, "summary": s, "limits": limit_status_summary(lim),
                      "top_risk": a.rc.head(3)[["pct"]].to_dict()["pct"],
                      "insights": a.insights(lim)[:2]})
    coverage = Price.objects.aggregate(first=Min("date"), last=Max("date"))
    return render(request, "web/home.html", {"cards": cards, "coverage": coverage,
                                             "n_assets": Price.objects.values("asset").distinct().count()})


@login_required
def overview(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return render(request, "web/overview.html", ctx)
    a = ctx["a"]
    s = ctx["summary"]
    rc = a.rc
    top = rc.head(10)
    sector = a.sector_weights
    sector_rc = a.sector_rc
    fd = a.factor_decomposition["exposures"]
    perf = a.performance
    # cumulative performance last 252 days vs benchmark
    pr = a.port_returns.iloc[-252:]
    cum = (1 + pr).cumprod() - 1
    bench = None
    if a.benchmark_returns is not None:
        br = a.benchmark_returns.reindex(pr.index).fillna(0.0)
        bench = (1 + br).cumprod() - 1
    charts = chart(
        sector_exposure={"labels": list(sector.index), "long": list(sector["long"]), "short": list(sector["short"]),
                         "risk": [float(sector_rc["pct"].get(sname, 0.0)) for sname in sector.index]},
        top_risk={"labels": list(top.index), "pct": list(top["pct"]), "weight": list(top["weight"])},
        factors={"labels": list(fd.index), "beta": list(fd["beta"]), "share": list(fd["pct_of_total_var"])},
        cum={"port": series_xy(cum), "bench": series_xy(bench) if bench is not None else None, "bench_name": s["benchmark"]},
        drawdown=series_xy(a.rolling()["drawdown"].iloc[-252:]),
    )
    ctx.update({
        "top_rc": top, "sector_weights": sector, "sector_rc": sector_rc, "factor_exposures": fd,
        "perf": perf, "concentration": a.concentration, "insights": a.insights(ctx["limit_results"]),
        "charts": charts, "worst_stress": sorted(
            [d for d in a.stress_historical + a.stress_hypothetical if d.get("available")],
            key=lambda d: d["total_pnl"])[:4],
        "liquidity_profile": a.liquidity_profile,
    })
    return render(request, "web/overview.html", ctx)
