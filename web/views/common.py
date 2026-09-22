"""Shared helpers for views."""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404

from core.models import Portfolio
from web.services import build_analyzer, jsonable, limit_status_summary, limits_for, parse_as_of


def get_portfolio(pk: int) -> Portfolio:
    return get_object_or_404(Portfolio, pk=pk)


def portfolio_context(pk: int, request=None) -> dict:
    """Portfolio + analyzer + limits: the context every portfolio page starts from.

    Honours an optional ``?as_of=YYYY-MM-DD`` query parameter so the whole app can be
    viewed as it would have looked on a past date (current holdings, historical prices)."""
    portfolio = get_portfolio(pk)
    as_of, as_of_error = (None, None)
    if request is not None:
        as_of, as_of_error = parse_as_of(request.GET.get("as_of"))
        if as_of_error:
            messages.warning(request, as_of_error)
    a = build_analyzer(portfolio, as_of=as_of)
    empty = len(a.positions) == 0
    ctx = {"portfolio": portfolio, "a": a, "empty": empty, "as_of": as_of,
           "as_of_qs": f"?as_of={as_of.date()}" if as_of is not None else "",
           "warnings": a.all_warnings() if not empty else []}
    if not empty:
        limit_results = a.check_limits(limits_for(portfolio))
        ctx.update({
            "summary": a.summary(),
            "limit_results": limit_results,
            "limit_summary": limit_status_summary(limit_results),
        })
    return ctx


def chart(**kwargs) -> dict:
    return jsonable(kwargs)
