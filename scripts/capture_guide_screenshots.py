"""
Capture the screenshots used by the user guide (README.md).

Walks every page of a running RiskDesk instance in a headless browser and saves
a full-page screenshot plus one screenshot per card (chart or table) into
docs/guide/. Re-run after UI changes so the documentation stays current.

    python manage.py runserver 127.0.0.1:8011
    python scripts/capture_guide_screenshots.py --base http://127.0.0.1:8011
"""
import argparse
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "guide"


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:48] or "card"


def wait_charts(page):
    try:
        page.wait_for_function(
            "Array.from(document.querySelectorAll('.chart')).every(c => c.querySelector('.plot-container') || c.offsetParent === null)",
            timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)


def capture_page(page, key, full=True, cards=True, kpi=True):
    wait_charts(page)
    if full:
        page.screenshot(path=str(OUT / f"{key}.png"), full_page=True)
    if kpi:
        for i, grid in enumerate(page.locator(".kpi-grid").all()):
            if grid.is_visible():
                grid.screenshot(path=str(OUT / f"{key}__kpis{'' if i == 0 else i + 1}.png"))
    if cards:
        seen = {}
        for card in page.locator(".card").all():
            if not card.is_visible():
                continue
            header = card.locator(":scope > .card-header").first
            title = header.evaluate("h => (h.childNodes[0] && h.childNodes[0].textContent || h.textContent || '').trim()") if header.count() else ""
            name = slug(title) if title else "card"
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name}-{seen[name]}"
            try:
                card.screenshot(path=str(OUT / f"{key}__{name}.png"))
            except Exception as exc:  # noqa: BLE001
                print(f"  skip card {name}: {exc}")
    print(f"captured {key}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    ap.add_argument("--user", default="cio")
    ap.add_argument("--password", default="riskdesk")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    B = args.base

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900}, device_scale_factor=1)

        # login page + sign in
        page.goto(f"{B}/login/")
        page.screenshot(path=str(OUT / "login.png"))
        page.fill("input[name=username]", args.user)
        page.fill("input[name=password]", args.password)
        page.click("form button.btn-primary")
        page.wait_for_url(f"{B}/")

        # home
        page.goto(f"{B}/")
        capture_page(page, "home")
        page.locator(".navbar").screenshot(path=str(OUT / "topbar.png"))

        # Flagship (pk=1) pages
        page.goto(f"{B}/p/1/")
        capture_page(page, "overview")
        page.locator(".rd-sidebar").screenshot(path=str(OUT / "sidebar.png"))
        page.locator(".rd-page-title").screenshot(path=str(OUT / "overview__header.png"))
        # maximised card demo
        page.locator(".card", has_text="Top risk contributors").locator(".rd-max").click()
        time.sleep(0.8)
        page.screenshot(path=str(OUT / "overview__maximised.png"))
        page.keyboard.press("Escape")
        time.sleep(0.3)
        # card tools close-up
        page.locator(".card", has_text="Risk limits").locator(":scope > .card-header").screenshot(path=str(OUT / "card-tools.png"))

        page.goto(f"{B}/p/1/?as_of=2025-04-08")
        wait_charts(page)
        page.screenshot(path=str(OUT / "overview__asof.png"))

        for key, path in [("positions", "positions/"), ("decomposition", "risk/decomposition/"), ("factors", "risk/factors/"),
                          ("tail", "risk/tail/"), ("stress", "risk/stress/?scenario=covid_crash"), ("correlation", "risk/correlation/"),
                          ("liquidity", "risk/liquidity/"), ("limits", "limits/"), ("report", "report/"), ("settings", "settings/"),
                          ("proposals", "trades/proposals/"), ("whatif-empty", "trades/whatif/")]:
            page.goto(f"{B}/p/1/{path}")
            capture_page(page, key)

        # stress: custom shock
        page.goto(f"{B}/p/1/risk/stress/")
        wait_charts(page)
        page.fill("input[name=MKT]", "-0.10")
        page.fill("input[name=RATES]", "-0.05")
        page.fill("input[name=OIL]", "0.20")
        page.click("form[data-busy] button.btn-primary")
        wait_charts(page)
        page.locator(".card", has_text="Build your own shock").screenshot(path=str(OUT / "stress__custom-shock-result.png"))

        # risk history: compute 60 days then capture
        page.goto(f"{B}/p/1/risk/history/")
        page.select_option("select[name=days]", "60")
        page.click("form[data-busy] button.btn-primary")
        page.wait_for_url(f"{B}/p/1/risk/history/")
        capture_page(page, "history")

        # what-if with results, then save as proposal
        page.goto(f"{B}/p/1/trades/whatif/")
        page.fill("input[name='ticker[]']", "NVDA")
        page.select_option("select[name='mode[]']", "target_weight")
        page.fill("input[name='value[]']", "0.04")
        page.click("text=Add row")
        page.locator("input[name='ticker[]']").nth(1).fill("AMD")
        page.locator("select[name='mode[]']").nth(1).select_option("delta_weight")
        page.locator("input[name='value[]']").nth(1).fill("0.03")
        page.fill("input[name=name]", "Trim NVDA, add AMD")
        page.fill("textarea[name=rationale]", "NVDA is 24% of risk on 7% of NAV; rotate part of it into AMD.")
        page.click("button[name=action][value=analyse]")
        capture_page(page, "whatif")
        page.click("button[name=action][value=save]")
        page.wait_for_url(re.compile(r".*/trades/proposals/\d+/"))
        capture_page(page, "proposal-detail")
        page.goto(f"{B}/p/1/trades/proposals/")
        capture_page(page, "proposals", kpi=False)

        # sizing with a new position
        page.goto(f"{B}/p/1/construct/sizing/")
        page.fill("input[name=new_ticker]", "AMD")
        page.click("#sz-form button.btn-primary")
        capture_page(page, "sizing")

        # optimiser on Multi-Asset (pk=2): form + results
        page.goto(f"{B}/p/2/construct/optimize/")
        capture_page(page, "optimize-form", cards=True)
        page.click("button[name=action][value=run]")
        capture_page(page, "optimize")

        # backtest on Multi-Asset
        page.goto(f"{B}/p/2/construct/backtest/")
        capture_page(page, "backtest-form")
        page.click("#bt-form button.btn-primary")
        capture_page(page, "backtest")

        # Concentrated Growth overview (many breaches)
        page.goto(f"{B}/p/3/")
        capture_page(page, "overview-concentrated", cards=False, kpi=True)

        # market data + asset detail + api docs
        page.goto(f"{B}/market/")
        capture_page(page, "market")
        page.goto(f"{B}/market/NVDA/")
        capture_page(page, "asset")
        page.goto(f"{B}/api-docs/")
        capture_page(page, "api", kpi=False)
        page.goto(f"{B}/api/portfolios/1/risk/")
        page.screenshot(path=str(OUT / "api__browsable.png"))
        page.goto(f"{B}/admin/")
        page.screenshot(path=str(OUT / "admin.png"))

        browser.close()
    print(f"done: {len(list(OUT.glob('*.png')))} images in {OUT}")


if __name__ == "__main__":
    main()
