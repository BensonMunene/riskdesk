from core.models import Portfolio


def portfolio_nav(request):
    """Expose the portfolio list and the active portfolio (from the URL) to every template."""
    if not request.user.is_authenticated:
        return {}
    portfolios = list(Portfolio.objects.only("id", "name"))
    active = None
    pk = request.resolver_match.kwargs.get("pk") if request.resolver_match else None
    if pk:
        active = next((p for p in portfolios if p.id == int(pk)), None)
    as_of_raw = request.GET.get("as_of", "")
    latest = earliest = ""
    if active is not None:
        try:
            from web.services import load_market_data
            close, _ = load_market_data()
            if not close.empty:
                latest = close.index[-1].strftime("%Y-%m-%d")
                earliest = close.index[min(len(close) - 1, 90)].strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001 - navigation must never break a page
            pass
    return {"nav_portfolios": portfolios, "active_portfolio": active, "nav_as_of": as_of_raw,
            "nav_as_of_qs": f"?as_of={as_of_raw}" if as_of_raw else "", "nav_latest": latest, "nav_earliest": earliest}
