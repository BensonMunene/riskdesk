"""Update prices from Yahoo Finance (yfinance) for all assets, or add new tickers.

    python manage.py fetch_prices                 # incremental update for every asset
    python manage.py fetch_prices --tickers NFLX CRM --start 2019-12-01   # add new names
"""
from datetime import timedelta

import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max

from core.models import Asset, Price


class Command(BaseCommand):
    help = "Fetch or update daily prices with yfinance."

    def add_arguments(self, parser):
        parser.add_argument("--tickers", nargs="*", help="Tickers to fetch (default: all assets)")
        parser.add_argument("--start", default="2019-12-01")

    def handle(self, *args, **opts):
        try:
            import yfinance as yf
        except ImportError:
            self.stderr.write("yfinance is not installed: pip install yfinance")
            return
        tickers = opts["tickers"] or list(Asset.objects.values_list("ticker", flat=True))
        for t in tickers:
            asset, created = Asset.objects.get_or_create(ticker=t.upper(), defaults={"name": t.upper()})
            if created:
                try:
                    info = yf.Ticker(t).info or {}
                    asset.name = info.get("shortName") or info.get("longName") or t
                    asset.sector = info.get("sector") or ""
                    asset.industry = info.get("industry") or ""
                    asset.market_cap = info.get("marketCap")
                    asset.country = info.get("country") or "United States"
                    asset.asset_class = "ETF" if info.get("quoteType") == "ETF" else "Equity"
                    asset.save()
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f"  reference lookup failed for {t}: {exc}")
            last = Price.objects.filter(asset=asset).aggregate(m=Max("date"))["m"]
            start = (last + timedelta(days=1)).isoformat() if last else opts["start"]
            df = yf.download(t, start=start, progress=False, auto_adjust=True)
            if df is None or df.empty:
                self.stdout.write(f"  {t}: up to date")
                continue
            close = df["Close"]
            vol = df["Volume"]
            if isinstance(close, pd.DataFrame):
                close, vol = close.iloc[:, 0], vol.iloc[:, 0]
            rows = [Price(asset=asset, date=d.date(), close=float(c), volume=int(vol.loc[d] or 0))
                    for d, c in close.dropna().items()]
            with transaction.atomic():
                Price.objects.bulk_create(rows, ignore_conflicts=True, batch_size=5000)
            self.stdout.write(f"  {t}: +{len(rows)} rows")
        self.stdout.write(self.style.SUCCESS("Done."))
