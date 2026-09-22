"""
Build the bundled sample market data set used by RiskDesk.

Downloads adjusted daily closes and volumes for the demo universe with
yfinance and writes wide CSV files into data/sample/. Also pulls basic
reference data (name, sector, industry, market cap) for each ticker.

Usage:
    python scripts/build_sample_data.py [--start 2019-12-01]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "sample"

STOCKS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "JPM", "BAC", "GS",
    "XOM", "CVX", "UNH", "JNJ", "LLY", "PG", "KO", "PEP", "HD", "COST",
    "CAT", "DE", "LMT", "NEE", "LIN", "V", "MA", "TSLA", "INTC", "BA",
    "KHC", "F", "GM", "PFE", "T", "VZ", "DIS", "NKE", "SBUX", "AMD",
]
ETFS = [
    "SPY", "QQQ", "IWM", "IWF", "IWD", "MTUM", "QUAL", "USMV", "TLT", "IEF",
    "HYG", "LQD", "UUP", "GLD", "USO", "XLK", "XLF", "XLE", "XLV", "XLP",
    "XLI", "XLU", "XLY", "XLC", "XLB", "XLRE", "EFA", "EEM", "VNQ",
]

# Fallback classification if yfinance .info is unavailable.
ETF_META = {
    "SPY": ("SPDR S&P 500 ETF", "Equity Index", "US Large Cap"),
    "QQQ": ("Invesco QQQ Trust", "Equity Index", "US Growth"),
    "IWM": ("iShares Russell 2000", "Equity Index", "US Small Cap"),
    "IWF": ("iShares Russell 1000 Growth", "Equity Index", "US Growth"),
    "IWD": ("iShares Russell 1000 Value", "Equity Index", "US Value"),
    "MTUM": ("iShares MSCI USA Momentum", "Equity Index", "Momentum"),
    "QUAL": ("iShares MSCI USA Quality", "Equity Index", "Quality"),
    "USMV": ("iShares MSCI USA Min Vol", "Equity Index", "Low Volatility"),
    "TLT": ("iShares 20+ Year Treasury", "Fixed Income", "Long Treasury"),
    "IEF": ("iShares 7-10 Year Treasury", "Fixed Income", "Intermediate Treasury"),
    "HYG": ("iShares iBoxx High Yield", "Fixed Income", "High Yield Credit"),
    "LQD": ("iShares iBoxx Inv Grade", "Fixed Income", "IG Credit"),
    "UUP": ("Invesco DB US Dollar Bullish", "Currency", "US Dollar"),
    "GLD": ("SPDR Gold Shares", "Commodity", "Gold"),
    "USO": ("United States Oil Fund", "Commodity", "Crude Oil"),
    "XLK": ("Technology Select Sector", "Equity Index", "Technology"),
    "XLF": ("Financial Select Sector", "Equity Index", "Financials"),
    "XLE": ("Energy Select Sector", "Equity Index", "Energy"),
    "XLV": ("Health Care Select Sector", "Equity Index", "Healthcare"),
    "XLP": ("Consumer Staples Select Sector", "Equity Index", "Consumer Staples"),
    "XLI": ("Industrial Select Sector", "Equity Index", "Industrials"),
    "XLU": ("Utilities Select Sector", "Equity Index", "Utilities"),
    "XLY": ("Consumer Discretionary Select", "Equity Index", "Consumer Discretionary"),
    "XLC": ("Communication Services Select", "Equity Index", "Communication Services"),
    "XLB": ("Materials Select Sector", "Equity Index", "Materials"),
    "XLRE": ("Real Estate Select Sector", "Equity Index", "Real Estate"),
    "EFA": ("iShares MSCI EAFE", "Equity Index", "Developed ex-US"),
    "EEM": ("iShares MSCI Emerging Markets", "Equity Index", "Emerging Markets"),
    "VNQ": ("Vanguard Real Estate ETF", "Equity Index", "Real Estate"),
}


def download_prices(tickers, start):
    print(f"Downloading {len(tickers)} tickers from {start} ...", flush=True)
    raw = yf.download(tickers, start=start, progress=False, auto_adjust=True,
                      group_by="column", threads=True)
    close = raw["Close"].copy()
    volume = raw["Volume"].copy()
    close.index = pd.to_datetime(close.index).tz_localize(None)
    volume.index = close.index
    close = close.sort_index().round(4)
    volume = volume.sort_index().fillna(0).astype("int64")
    # Drop days where everything is missing (exchange holidays picked up by yf)
    close = close.dropna(how="all")
    volume = volume.loc[close.index]
    return close, volume


def fetch_reference(tickers):
    rows = []
    for i, t in enumerate(tickers, 1):
        name, sector, industry, mcap, country, cls = t, "", "", None, "US", "Equity"
        if t in ETF_META:
            name, cls, industry = ETF_META[t]
            sector = industry
            cls = {"Equity Index": "ETF", "Fixed Income": "Fixed Income",
                   "Currency": "Currency", "Commodity": "Commodity"}[cls]
        else:
            try:
                info = yf.Ticker(t).info or {}
                name = info.get("shortName") or info.get("longName") or t
                sector = info.get("sector") or ""
                industry = info.get("industry") or ""
                mcap = info.get("marketCap")
                country = info.get("country") or "US"
            except Exception as exc:  # noqa: BLE001
                print(f"  info failed for {t}: {exc}", file=sys.stderr)
            time.sleep(0.2)
        rows.append({"ticker": t, "name": name, "asset_class": cls, "sector": sector,
                     "industry": industry, "market_cap": mcap, "country": country,
                     "currency": "USD"})
        if i % 10 == 0:
            print(f"  reference {i}/{len(tickers)}", flush=True)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-12-01")
    ap.add_argument("--skip-reference", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    tickers = STOCKS + ETFS
    close, volume = download_prices(tickers, args.start)
    missing = [t for t in tickers if t not in close.columns or close[t].isna().all()]
    if missing:
        print(f"WARNING: no data for {missing}", file=sys.stderr)
    close.to_csv(OUT / "prices_close.csv", index_label="date")
    volume.to_csv(OUT / "prices_volume.csv", index_label="date")
    print(f"Wrote prices: {close.shape[0]} days x {close.shape[1]} tickers "
          f"({close.index.min().date()} to {close.index.max().date()})")

    if not args.skip_reference:
        ref = fetch_reference(tickers)
        ref.to_csv(OUT / "assets.csv", index=False)
        print(f"Wrote reference data for {len(ref)} assets")

    meta = {"start": str(close.index.min().date()), "end": str(close.index.max().date()),
            "tickers": list(close.columns), "source": "yfinance (adjusted close)"}
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
