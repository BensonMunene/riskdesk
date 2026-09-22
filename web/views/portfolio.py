import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Asset, Portfolio, Position, RiskLimit
from web.forms import AnalyticsSettingsForm, PortfolioForm, PositionForm, PositionImportForm, RiskLimitForm

from .common import portfolio_context


@login_required
def portfolio_create(request):
    form = PortfolioForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        p = form.save(commit=False)
        p.owner = request.user
        p.save()
        messages.success(request, f"Created portfolio '{p.name}'. Add positions next.")
        return redirect("positions", pk=p.pk)
    return render(request, "web/portfolio_form.html", {"form": form, "title": "New portfolio"})


@login_required
def portfolio_edit(request, pk):
    portfolio = get_object_or_404(Portfolio, pk=pk)
    form = PortfolioForm(request.POST or None, instance=portfolio)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Portfolio updated.")
        return redirect("overview", pk=pk)
    return render(request, "web/portfolio_form.html", {"form": form, "title": f"Edit {portfolio.name}", "portfolio": portfolio})


@login_required
def portfolio_delete(request, pk):
    portfolio = get_object_or_404(Portfolio, pk=pk)
    if request.method == "POST":
        name = portfolio.name
        portfolio.delete()
        messages.success(request, f"Deleted '{name}'.")
        return redirect("home")
    return render(request, "web/confirm_delete.html", {"portfolio": portfolio, "object": portfolio, "kind": "portfolio"})


@login_required
def positions(request, pk):
    ctx = portfolio_context(pk, request)
    portfolio = ctx["portfolio"]
    form = PositionForm(request.POST or None)
    if request.method == "POST":
        if "update_qty" in request.POST:
            with transaction.atomic():
                for key, val in request.POST.items():
                    if key.startswith("qty_"):
                        pos = Position.objects.filter(portfolio=portfolio, pk=int(key[4:])).first()
                        if pos is None:
                            continue
                        try:
                            q = float(val)
                        except ValueError:
                            continue
                        if q == 0:
                            pos.delete()
                        elif q != pos.quantity:
                            pos.quantity = q
                            pos.save()
                if "cash" in request.POST:
                    try:
                        portfolio.cash = float(request.POST["cash"])
                        portfolio.save(update_fields=["cash", "updated_at"])
                    except ValueError:
                        pass
            messages.success(request, "Positions updated.")
            return redirect("positions", pk=pk)
        if form.is_valid():
            asset = Asset.objects.get(ticker=form.cleaned_data["ticker"])
            pos, created = Position.objects.get_or_create(portfolio=portfolio, asset=asset,
                                                          defaults={"quantity": form.cleaned_data["quantity"],
                                                                    "avg_cost": form.cleaned_data["avg_cost"],
                                                                    "note": form.cleaned_data["note"]})
            if not created:
                pos.quantity = form.cleaned_data["quantity"]
                pos.avg_cost = form.cleaned_data["avg_cost"]
                pos.note = form.cleaned_data["note"]
                pos.save()
            messages.success(request, f"{'Added' if created else 'Updated'} {asset.ticker}.")
            return redirect("positions", pk=pk)
    rows = []
    a = ctx["a"]
    for pos in portfolio.positions.select_related("asset"):
        t = pos.asset.ticker
        px = float(a.last_prices.get(t, float("nan"))) if t in a.prices.columns else float("nan")
        mv = pos.quantity * px if px == px else float("nan")
        rows.append({"pos": pos, "price": px, "market_value": mv,
                     "weight": mv / a.nav if a.nav and mv == mv else float("nan"),
                     "pnl": (px - pos.avg_cost) * pos.quantity if pos.avg_cost and px == px else None,
                     "risk_share": float(a.rc["pct"].get(t, 0.0)) if not ctx["empty"] else None,
                     "has_prices": t in a.prices.columns})
    ctx.update({"form": form, "rows": rows, "import_form": PositionImportForm(),
                "universe": list(a.prices.columns)})
    return render(request, "web/positions.html", ctx)


@login_required
def positions_import(request, pk):
    portfolio = get_object_or_404(Portfolio, pk=pk)
    form = PositionImportForm(request.POST, request.FILES)
    if request.method == "POST" and form.is_valid():
        text = form.cleaned_data["file"].read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        reader.fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
        if "ticker" not in reader.fieldnames or "quantity" not in reader.fieldnames:
            messages.error(request, "CSV needs 'ticker' and 'quantity' columns.")
            return redirect("positions", pk=pk)
        added, skipped = 0, []
        with transaction.atomic():
            if form.cleaned_data["replace"]:
                portfolio.positions.all().delete()
            for row in reader:
                t = (row.get("ticker") or "").strip().upper()
                if not t:
                    continue
                asset = Asset.objects.filter(ticker=t).first()
                if asset is None:
                    skipped.append(t)
                    continue
                try:
                    q = float(row.get("quantity") or 0)
                except ValueError:
                    skipped.append(t)
                    continue
                cost = row.get("avg_cost")
                pos, _ = Position.objects.update_or_create(
                    portfolio=portfolio, asset=asset,
                    defaults={"quantity": q, "avg_cost": float(cost) if cost else None, "note": row.get("note") or ""})
                added += 1
        messages.success(request, f"Imported {added} positions." + (f" Skipped (not in universe): {', '.join(skipped)}" if skipped else ""))
    return redirect("positions", pk=pk)


@login_required
def position_delete(request, pk, pos_id):
    pos = get_object_or_404(Position, pk=pos_id, portfolio_id=pk)
    if request.method == "POST":
        pos.delete()
        messages.success(request, f"Removed {pos.asset.ticker}.")
    return redirect("positions", pk=pk)


@login_required
def analytics_settings(request, pk):
    portfolio = get_object_or_404(Portfolio, pk=pk)
    current = portfolio.analytics_settings()
    current["var_confidence"] = str(current.get("var_confidence", 0.99))
    form = AnalyticsSettingsForm(request.POST or None, initial=current)
    if request.method == "POST" and form.is_valid():
        cd = dict(form.cleaned_data)
        cd["var_confidence"] = float(cd["var_confidence"])
        if cd.get("target_beta") is None:
            cd.pop("target_beta", None)
            portfolio.settings.pop("target_beta", None)
        portfolio.settings = {**portfolio.settings, **cd}
        portfolio.save(update_fields=["settings", "updated_at"])
        messages.success(request, "Analytics settings saved.")
        return redirect("overview", pk=pk)
    return render(request, "web/settings.html", {"form": form, "portfolio": portfolio})


@login_required
def limits(request, pk):
    ctx = portfolio_context(pk, request)
    portfolio = ctx["portfolio"]
    form = RiskLimitForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        lim = form.save(commit=False)
        lim.portfolio = portfolio
        lim.save()
        messages.success(request, f"Added limit '{lim.display_label}'.")
        return redirect("limits", pk=pk)
    ctx.update({"form": form, "all_limits": portfolio.limits.all(),
                "sectors": sorted(set(portfolio.positions.values_list("asset__sector", flat=True)))})
    return render(request, "web/limits.html", ctx)


@login_required
def limit_delete(request, pk, limit_id):
    lim = get_object_or_404(RiskLimit, pk=limit_id, portfolio_id=pk)
    if request.method == "POST":
        lim.delete()
        messages.success(request, "Limit removed.")
    return redirect("limits", pk=pk)
