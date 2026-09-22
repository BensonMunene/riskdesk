"""Load the bundled sample universe, prices and demo portfolios.

    python manage.py load_sample_data            # idempotent; refreshes prices and portfolios
    python manage.py load_sample_data --reset    # wipe portfolios first
"""
import json

import numpy as np
import pandas as pd
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from analytics.factors import required_tickers
from core.models import Asset, Portfolio, Position, Price, RiskLimit

DEMO_USER = ("cio", "riskdesk")


class Command(BaseCommand):
    help = "Load sample assets, prices and demo portfolios into the database."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing portfolios first")
        parser.add_argument("--skip-prices", action="store_true")

    def handle(self, *args, **opts):
        d = settings.SAMPLE_DATA_DIR
        assets = pd.read_csv(d / "assets.csv").fillna("")
        factor_etfs = set(required_tickers())

        self.stdout.write("Loading assets ...")
        asset_objs = {}
        for _, r in assets.iterrows():
            obj, _ = Asset.objects.update_or_create(
                ticker=r["ticker"],
                defaults={
                    "name": r["name"] or r["ticker"],
                    "asset_class": r["asset_class"] or "Equity",
                    "sector": r["sector"],
                    "industry": r["industry"],
                    "country": r["country"] or "United States",
                    "currency": r["currency"] or "USD",
                    "market_cap": float(r["market_cap"]) if r["market_cap"] not in ("", None) else None,
                    "is_factor_etf": r["ticker"] in factor_etfs,
                },
            )
            asset_objs[r["ticker"]] = obj
        self.stdout.write(f"  {len(asset_objs)} assets")

        if not opts["skip_prices"]:
            self.load_prices(d, asset_objs)

        close = pd.read_csv(d / "prices_close.csv", index_col=0, parse_dates=True)
        last = close.ffill().iloc[-1]
        # average cost = price ~6 months ago so the demo books carry realistic unrealised P&L
        self.cost_basis = close.ffill().iloc[-126] if len(close) > 126 else last

        if opts["reset"]:
            Portfolio.objects.all().delete()

        User = get_user_model()
        user, created = User.objects.get_or_create(username=DEMO_USER[0], defaults={
            "is_staff": True, "is_superuser": True, "first_name": "Demo", "last_name": "CIO"})
        if created:
            user.set_password(DEMO_USER[1])
            user.save()
            self.stdout.write(f"  created demo user {DEMO_USER[0]} / {DEMO_USER[1]}")

        portfolios = json.loads((d / "portfolios.json").read_text())
        for p in portfolios:
            self.load_portfolio(p, last, asset_objs, user)
        self.stdout.write(self.style.SUCCESS("Sample data loaded."))

    def load_prices(self, d, asset_objs):
        close = pd.read_csv(d / "prices_close.csv", index_col=0, parse_dates=True)
        volume = pd.read_csv(d / "prices_volume.csv", index_col=0, parse_dates=True)
        existing = dict(Price.objects.values_list("asset__ticker").annotate(n=Count("id")))
        self.stdout.write("Loading prices ...")
        total = 0
        for t in close.columns:
            if t not in asset_objs:
                continue
            if existing.get(t, 0) >= close[t].notna().sum():
                continue
            asset = asset_objs[t]
            have = set(Price.objects.filter(asset=asset).values_list("date", flat=True))
            rows = []
            s = close[t].dropna()
            v = volume[t].reindex(s.index).fillna(0)
            for date, px in s.items():
                dd = date.date()
                if dd in have:
                    continue
                rows.append(Price(asset=asset, date=dd, close=float(px), volume=int(v[date])))
            with transaction.atomic():
                Price.objects.bulk_create(rows, batch_size=5000)
            total += len(rows)
        self.stdout.write(f"  {total} price rows inserted ({Price.objects.count()} total)")

    def load_portfolio(self, p, last, asset_objs, user):
        port, _ = Portfolio.objects.update_or_create(
            name=p["name"],
            defaults={"description": p.get("description", ""), "benchmark": p.get("benchmark", "SPY"),
                      "settings": p.get("settings", {}), "owner": user},
        )
        port.positions.all().delete()
        port.limits.all().delete()
        nav = float(p["nav"])
        mv_total = 0.0
        for t, w in p["weights"].items():
            if t not in asset_objs or t not in last.index or np.isnan(last[t]):
                self.stderr.write(f"  skipping {t}: no price")
                continue
            px = float(last[t])
            qty = float(np.floor(abs(w) * nav / px)) * (1 if w >= 0 else -1)
            cost = float(self.cost_basis.get(t, px))
            Position.objects.create(portfolio=port, asset=asset_objs[t], quantity=qty,
                                    avg_cost=cost if not np.isnan(cost) else px)
            mv_total += qty * px
        port.cash = nav - mv_total
        port.save()
        for lim in p.get("limits", []):
            RiskLimit.objects.create(portfolio=port, metric=lim["metric"], scope=lim.get("scope", "") or "",
                                     operator=lim.get("operator", "lte"), threshold=float(lim["threshold"]),
                                     label=lim.get("label", ""))
        self.stdout.write(f"  portfolio '{port.name}': {port.positions.count()} positions, {port.limits.count()} limits, NAV {nav:,.0f}")
