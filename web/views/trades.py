import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Asset, Position, TradeLine, TradeProposal
from web.compare import compare, trade_table
from web.forms import WhatIfForm
from web.services import asset_meta_frame, jsonable, limits_for, parse_trade_lines

from .common import chart, portfolio_context

SESSION_KEY = "pending_trades"


def _rows_from_post(post):
    tickers = post.getlist("ticker[]")
    modes = post.getlist("mode[]")
    values = post.getlist("value[]")
    return [{"ticker": t, "mode": m, "value": v} for t, m, v in zip(tickers, modes, values)]


@login_required
def whatif(request, pk):
    ctx = portfolio_context(pk, request)
    if ctx["empty"]:
        return redirect("positions", pk=pk)
    a = ctx["a"]
    portfolio = ctx["portfolio"]
    form = WhatIfForm(request.POST or None)
    rows, trades, errors, result = [], {}, [], None

    # preload rows from the optimiser / sizing pages or a proposal
    if request.method == "GET":
        pending = request.session.pop(SESSION_KEY, None)
        if pending:
            rows = [{"ticker": t, "mode": "shares", "value": q} for t, q in pending.get("trades", {}).items()]
            form = WhatIfForm(initial={"name": pending.get("name", ""), "rationale": pending.get("rationale", "")})
        elif request.GET.get("proposal"):
            prop = get_object_or_404(TradeProposal, pk=request.GET["proposal"], portfolio=portfolio)
            rows = [{"ticker": t, "mode": "shares", "value": q} for t, q in prop.trades_dict().items()]
            form = WhatIfForm(initial={"name": prop.name, "rationale": prop.rationale})
        elif request.GET.get("ticker"):
            rows = [{"ticker": request.GET["ticker"], "mode": request.GET.get("mode", "shares"), "value": request.GET.get("value", "")}]

    if request.method == "POST":
        rows = _rows_from_post(request.POST)
        trades, errors = parse_trade_lines(rows, a)
        for e in errors:
            messages.warning(request, e)
        if trades:
            b = a.with_trades(trades, asset_meta_frame())
            limits = limits_for(portfolio)
            cmp_ = compare(a, b, limits)
            after_limits = b.check_limits(limits)
            result = {
                "compare": cmp_,
                "trades": trade_table(a, trades),
                "insights_after": b.insights(after_limits),
                "rc_after": b.rc.head(10),
                "summary_after": b.summary(),
                "charts": chart(
                    rc={"labels": [r["ticker"] for r in cmp_["rc"][:15]],
                        "before": [r["pct_before"] for r in cmp_["rc"][:15]],
                        "after": [r["pct_after"] for r in cmp_["rc"][:15]]},
                    factors={"labels": [r["factor"] for r in cmp_["factors"]],
                             "before": [r["beta_before"] for r in cmp_["factors"]],
                             "after": [r["beta_after"] for r in cmp_["factors"]]},
                    sectors={"labels": [r["sector"] for r in cmp_["sectors"]],
                             "before": [r["net_before"] for r in cmp_["sectors"]],
                             "after": [r["net_after"] for r in cmp_["sectors"]]},
                ),
            }
            if request.POST.get("action") == "save" and form.is_valid():
                name = form.cleaned_data["name"] or f"What-if {len(trades)} trade(s)"
                with transaction.atomic():
                    prop = TradeProposal.objects.create(
                        portfolio=portfolio, name=name, rationale=form.cleaned_data["rationale"],
                        created_by=request.user, source=request.POST.get("source", "manual"),
                        snapshot=jsonable({"before": a.summary(), "after": b.summary(), "verdict": cmp_["verdict"],
                                           "new_breaches": cmp_["new_breaches"], "fixed_breaches": cmp_["fixed_breaches"]}))
                    for t, dq in trades.items():
                        TradeLine.objects.create(proposal=prop, asset=Asset.objects.get(ticker=t), quantity_delta=dq)
                messages.success(request, f"Saved proposal '{prop.name}'.")
                return redirect("proposal_detail", pk=pk, proposal_id=prop.id)
        elif not errors:
            messages.info(request, "Add at least one trade line.")

    universe = list(a.prices.columns)
    ctx.update({"form": form, "rows": rows or [{"ticker": "", "mode": "shares", "value": ""}], "result": result,
                "universe": universe, "holdings": list(a.positions.index)})
    return render(request, "web/whatif.html", ctx)


@login_required
def proposals(request, pk):
    ctx = portfolio_context(pk, request)
    props = ctx["portfolio"].proposals.prefetch_related("lines__asset")
    ctx.update({"proposals": props})
    return render(request, "web/proposals.html", ctx)


@login_required
def proposal_detail(request, pk, proposal_id):
    ctx = portfolio_context(pk, request)
    prop = get_object_or_404(TradeProposal, pk=proposal_id, portfolio=ctx["portfolio"])
    result = None
    if not ctx["empty"]:
        a = ctx["a"]
        trades = prop.trades_dict()
        try:
            b = a.with_trades(trades, asset_meta_frame())
            limits = limits_for(ctx["portfolio"])
            result = {"compare": compare(a, b, limits), "trades": trade_table(a, trades),
                      "insights_after": b.insights(b.check_limits(limits))}
        except ValueError as exc:
            messages.warning(request, str(exc))
    ctx.update({"proposal": prop, "result": result, "statuses": TradeProposal.STATUS})
    return render(request, "web/proposal_detail.html", ctx)


@login_required
def proposal_status(request, pk, proposal_id):
    prop = get_object_or_404(TradeProposal, pk=proposal_id, portfolio_id=pk)
    if request.method == "POST":
        status = request.POST.get("status")
        if status in dict(TradeProposal.STATUS):
            prop.status = status
            prop.save(update_fields=["status", "updated_at"])
            messages.success(request, f"Proposal marked {prop.get_status_display().lower()}.")
    return redirect("proposal_detail", pk=pk, proposal_id=proposal_id)


@login_required
def proposal_apply(request, pk, proposal_id):
    """Book the proposal's trades into the live positions (marks it executed)."""
    ctx = portfolio_context(pk, request)
    prop = get_object_or_404(TradeProposal, pk=proposal_id, portfolio=ctx["portfolio"])
    if request.method != "POST":
        return redirect("proposal_detail", pk=pk, proposal_id=proposal_id)
    a = ctx["a"]
    portfolio = ctx["portfolio"]
    with transaction.atomic():
        traded_value = 0.0
        for line in prop.lines.select_related("asset"):
            pos, _ = Position.objects.get_or_create(portfolio=portfolio, asset=line.asset, defaults={"quantity": 0.0})
            px = float(a.last_prices.get(line.asset.ticker, line.limit_price or 0.0))
            pos.quantity += line.quantity_delta
            traded_value += line.quantity_delta * px
            if abs(pos.quantity) < 1e-9:
                pos.delete()
            else:
                pos.save()
        portfolio.cash -= traded_value
        portfolio.save(update_fields=["cash", "updated_at"])
        prop.status = "executed"
        prop.save(update_fields=["status", "updated_at"])
    messages.success(request, f"Booked {prop.lines.count()} trade(s) into {portfolio.name}; cash adjusted by {-traded_value:,.0f}.")
    return redirect("overview", pk=pk)
