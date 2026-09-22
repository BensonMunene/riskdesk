"""Before/after comparison of two PortfolioAnalyzer instances (what-if, optimiser, proposals)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.engine import PortfolioAnalyzer
from web.services import jsonable


def _row(label, before, after, fmt="pct", better="lower"):
    delta = None
    if before is not None and after is not None and not (np.isnan(before) or np.isnan(after)):
        delta = after - before
    return {"label": label, "before": before, "after": after, "delta": delta, "fmt": fmt, "better": better}


def compare(a: PortfolioAnalyzer, b: PortfolioAnalyzer, limits: list[dict]) -> dict:
    sa, sb = a.summary(), b.summary()
    metrics = [
        _row("NAV", sa["nav"], sb["nav"], "money", "none"),
        _row("Cash", sa["cash"], sb["cash"], "money", "none"),
        _row("Gross exposure", sa["gross_exposure"], sb["gross_exposure"], "pct", "none"),
        _row("Net exposure", sa["net_exposure"], sb["net_exposure"], "pct", "none"),
        _row("Long exposure", sa["long_exposure"], sb["long_exposure"], "pct", "none"),
        _row("Short exposure", sa["short_exposure"], sb["short_exposure"], "pct", "none"),
        _row("Positions", sa["n_positions"], sb["n_positions"], "int", "none"),
        _row("Annualised vol", sa["vol"], sb["vol"], "pct", "lower"),
        _row(f"Beta to {sa['benchmark']}", sa["beta"], sb["beta"], "num", "none"),
        _row(f"VaR {int(sa['var_confidence'] * 100)}% {sa['var_horizon_days']}d", sa["var"], sb["var"], "pct", "lower"),
        _row("VaR ($)", sa["var_dollar"], sb["var_dollar"], "money", "lower"),
        _row("Expected shortfall", sa["cvar"], sb["cvar"], "pct", "lower"),
        _row("Sharpe (pro forma)", sa["sharpe"], sb["sharpe"], "num", "higher"),
        _row("Max drawdown (pro forma)", sa["max_drawdown"], sb["max_drawdown"], "pct", "higher"),
        _row("Effective # positions", a.concentration["effective_n"], b.concentration["effective_n"], "num", "higher"),
        _row("Diversification ratio", a.concentration["div_ratio"], b.concentration["div_ratio"], "num", "higher"),
        _row("Effective # bets", a.concentration["enb"], b.concentration["enb"], "num", "higher"),
        _row("Top-5 share of gross", a.concentration["top5"], b.concentration["top5"], "pct", "lower"),
        _row("Max single-name risk share", float(a.rc["pct"].max()), float(b.rc["pct"].max()), "pct", "lower"),
        _row("Systematic share of variance", a.factor_decomposition["systematic_share"],
             b.factor_decomposition["systematic_share"], "pct", "none"),
    ]
    # risk contributions: union of tickers whose weight or risk share changed
    ra, rb = a.rc, b.rc
    tickers = sorted(set(ra.index) | set(rb.index))
    rc_rows = []
    for t in tickers:
        wa = float(ra["weight"].get(t, 0.0)); wb = float(rb["weight"].get(t, 0.0))
        pa = float(ra["pct"].get(t, 0.0)); pb = float(rb["pct"].get(t, 0.0))
        ca = float(ra["ccr"].get(t, 0.0)); cb = float(rb["ccr"].get(t, 0.0))
        if abs(wa - wb) > 1e-6 or abs(pa - pb) > 0.002:
            rc_rows.append({"ticker": t, "weight_before": wa, "weight_after": wb, "pct_before": pa, "pct_after": pb,
                            "ccr_before": ca, "ccr_after": cb, "changed": abs(wa - wb) > 1e-6})
    rc_rows.sort(key=lambda r: -abs(r["pct_after"] - r["pct_before"]))
    # sectors
    swa, swb = a.sector_weights, b.sector_weights
    sra, srb = a.sector_rc, b.sector_rc
    sectors = sorted(set(swa.index) | set(swb.index))
    sector_rows = [{"sector": s,
                    "net_before": float(swa["net"].get(s, 0.0)), "net_after": float(swb["net"].get(s, 0.0)),
                    "gross_before": float(swa["gross"].get(s, 0.0)), "gross_after": float(swb["gross"].get(s, 0.0)),
                    "risk_before": float(sra["pct"].get(s, 0.0)), "risk_after": float(srb["pct"].get(s, 0.0))}
                   for s in sectors]
    # factors
    fa, fb = a.factor_decomposition["exposures"], b.factor_decomposition["exposures"]
    factor_rows = [{"factor": f, "beta_before": float(fa["beta"].get(f, 0.0)), "beta_after": float(fb["beta"].get(f, 0.0)),
                    "var_before": float(fa["pct_of_total_var"].get(f, 0.0)), "var_after": float(fb["pct_of_total_var"].get(f, 0.0))}
                   for f in fa.index]
    # limits
    la = {l["id"]: l for l in a.check_limits(limits)}
    lb = {l["id"]: l for l in b.check_limits(limits)}
    limit_rows = [{"label": la[k]["label"], "threshold": la[k]["display_threshold"], "before": la[k]["display_value"],
                   "after": lb[k]["display_value"], "status_before": la[k]["status"], "status_after": lb[k]["status"],
                   "fixed": la[k]["status"] == "breach" and lb[k]["status"] != "breach",
                   "broken": la[k]["status"] != "breach" and lb[k]["status"] == "breach"} for k in la]
    # stress
    stress_rows = []
    for x, y in zip(a.stress_historical + a.stress_hypothetical, b.stress_historical + b.stress_hypothetical):
        if x.get("available") and y.get("available"):
            stress_rows.append({"name": x["name"], "kind": x["kind"], "before": x["total_pnl"], "after": y["total_pnl"],
                                "before_dollar": x["total_dollar"], "after_dollar": y["total_dollar"]})
    stress_rows.sort(key=lambda r: r["after"])
    new_breaches = [r for r in limit_rows if r["broken"]]
    fixed = [r for r in limit_rows if r["fixed"]]
    verdict = "green"
    if new_breaches:
        verdict = "red"
    elif sb["var"] > sa["var"] * 1.1 or sb["vol"] > sa["vol"] * 1.1:
        verdict = "amber"
    return jsonable({
        "metrics": metrics, "rc": rc_rows, "sectors": sector_rows, "factors": factor_rows,
        "limits": limit_rows, "stress": stress_rows, "new_breaches": new_breaches, "fixed_breaches": fixed,
        "verdict": verdict,
    })


def trade_table(a: PortfolioAnalyzer, trades: dict[str, float]) -> list[dict]:
    rows = []
    for t, dq in trades.items():
        px = float(a.last_prices[t]) if t in a.prices.columns else float("nan")
        cur = float(a.positions["quantity"].get(t, 0.0))
        rows.append({"ticker": t, "side": "BUY" if dq > 0 else "SELL", "shares": dq, "price": px,
                     "value": dq * px, "current_shares": cur, "new_shares": cur + dq,
                     "weight_before": float(a.weights.get(t, 0.0)),
                     "weight_after": (cur + dq) * px / a.nav if a.nav else float("nan"),
                     "name": str(a.positions["name"].get(t, t))})
    rows.sort(key=lambda r: -abs(r["value"]))
    return jsonable(rows)
