"""Generate the weekly client report (HTML + PDF) from a client config file.

    python manage.py weekly_report --client clients/<client>.json
    python manage.py weekly_report --client clients/<client>.json --date 2026-09-08   # re-create a past week
    python manage.py weekly_report --client clients/<client>.json --no-pdf --no-events

Weekly routine:
    python manage.py fetch_prices                      # refresh prices (and add any new client tickers)
    python manage.py weekly_report --client clients/<client>.json
Output: reports/<client-slug>/<date>/report.html, report.pdf, state.json, data.json
Start from data/sample/weekly_client_example.json; clients/ and reports/ are git-ignored (private).
"""
import datetime as dt
import json
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string

from analytics.factors import required_tickers
from analytics.report import build_weekly_report, to_jsonable
from core.models import Price
from web.services import asset_meta_frame, invalidate_market_cache, load_market_data


def fetch_events(tickers: list[str], as_of: pd.Timestamp, stdout=None) -> dict:
    """Next earnings date per ticker from Yahoo Finance; failures are simply blank."""
    out = {}
    try:
        import yfinance as yf
    except ImportError:
        return out
    for t in tickers:
        try:
            ed = yf.Ticker(t).get_earnings_dates(limit=12)
            if ed is None or len(ed) == 0:
                continue
            idx = ed.index.tz_localize(None) if ed.index.tz is not None else ed.index
            future = [d for d in idx if d.normalize() >= as_of.normalize()]
            if future:
                nxt = min(future)
                out[t] = {"next_earnings": nxt.strftime("%Y-%m-%d"), "days": int((nxt.normalize() - as_of.normalize()).days)}
        except Exception as exc:  # noqa: BLE001
            if stdout:
                stdout.write(f"  earnings lookup failed for {t}: {exc}")
    return out


def previous_state(client_dir: Path, as_of: pd.Timestamp) -> dict | None:
    """Most recent state.json strictly before the report date."""
    best = None
    for d in sorted(client_dir.glob("*/state.json")):
        try:
            when = pd.Timestamp(d.parent.name)
        except ValueError:
            continue
        if when < as_of.normalize():
            best = d
    if best is None:
        return None
    return json.loads(best.read_text(encoding="utf-8"))


def html_to_pdf(html_path: Path, pdf_path: Path, stdout=None) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if stdout:
            stdout.write("  playwright not installed: pip install playwright && python -m playwright install chromium")
        return False
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 1200})
        page.goto(html_path.resolve().as_uri())
        try:
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.chart')).every(c => c.querySelector('.plot-container'))", timeout=30000)
        except Exception:  # noqa: BLE001
            if stdout:
                stdout.write("  warning: some charts did not finish rendering before the PDF was printed")
        page.wait_for_timeout(600)
        page.pdf(path=str(pdf_path), format="A4", print_background=True, scale=0.9,
                 margin={"top": "10mm", "bottom": "12mm", "left": "9mm", "right": "9mm"},
                 display_header_footer=True,
                 header_template="<div></div>",
                 footer_template=("<div style='font-size:8px;color:#888;width:100%;padding:0 9mm;display:flex;justify-content:space-between'>"
                                  "<span>RiskDesk weekly report · confidential</span><span>page <span class='pageNumber'></span> of <span class='totalPages'></span></span></div>"))
        browser.close()
    return True


class Command(BaseCommand):
    help = "Build the weekly client report (HTML and PDF) from a client config."

    def add_arguments(self, parser):
        parser.add_argument("--client", required=True, help="Path to the client config JSON")
        parser.add_argument("--date", help="Report as-of date (YYYY-MM-DD); default = latest price date")
        parser.add_argument("--out", default="reports", help="Output root folder")
        parser.add_argument("--no-pdf", action="store_true")
        parser.add_argument("--no-events", action="store_true", help="Skip the earnings-date lookup (no network)")
        parser.add_argument("--no-fetch", action="store_true", help="Do not download prices for missing tickers")

    def handle(self, *args, **opts):
        cfg_path = Path(opts["client"])
        if not cfg_path.exists():
            raise CommandError(f"Config not found: {cfg_path}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        needed = list(dict.fromkeys(cfg["tickers"] + [cfg.get("benchmark", "SPY")] + required_tickers()))
        have = set(Price.objects.values_list("asset__ticker", flat=True).distinct())
        missing = [t for t in needed if t not in have]
        if missing and not opts["no_fetch"]:
            self.stdout.write(f"Fetching prices for {', '.join(missing)} ...")
            call_command("fetch_prices", tickers=missing)
            invalidate_market_cache()
        close, volume = load_market_data()
        if close.empty:
            raise CommandError("No price data loaded. Run load_sample_data or fetch_prices first.")
        as_of = pd.Timestamp(opts["date"]) if opts["date"] else close.index[-1]
        if as_of > close.index[-1]:
            raise CommandError(f"No prices after {close.index[-1].date()}")
        as_of = close.index[close.index <= as_of][-1]

        out_root = Path(opts["out"]) / cfg.get("slug", "client")
        out_dir = out_root / as_of.strftime("%Y-%m-%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        prev = previous_state(out_root, as_of)
        events = {} if opts["no_events"] else fetch_events(cfg["tickers"], as_of, self.stdout)

        self.stdout.write(f"Building report for {cfg.get('client')} as of {as_of.date()}" + (f" (previous: {prev['as_of']})" if prev else " (first report)"))
        report = build_weekly_report(cfg, close, volume, asset_meta_frame(), prev_state=prev, events=events, as_of=as_of)
        for w in report["warnings"]:
            self.stdout.write(self.style.WARNING(f"  warning: {w}"))

        rd_js = (settings.BASE_DIR / "static" / "js" / "riskdesk.js").read_text(encoding="utf-8")
        html = render_to_string("report/weekly.html", {"r": report, "rd_js": rd_js, "charts": report["charts"]})
        html_path = out_dir / "report.html"
        html_path.write_text(html, encoding="utf-8")
        (out_dir / "state.json").write_text(json.dumps(report["state"], indent=1), encoding="utf-8")
        (out_dir / "data.json").write_text(json.dumps(to_jsonable({k: v for k, v in report.items() if k != "state"}), indent=1), encoding="utf-8")
        self.stdout.write(f"  HTML: {html_path}")
        if not opts["no_pdf"]:
            pdf_path = out_dir / "report.pdf"
            if html_to_pdf(html_path, pdf_path, self.stdout):
                self.stdout.write(f"  PDF:  {pdf_path}")
        self.stdout.write(self.style.SUCCESS("Done."))
