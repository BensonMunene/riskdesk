"""Rule-based insight engine.

Turns the numbers into short, specific, actionable statements a CIO can act on,
each with a concrete suggested change. Rules are deliberately transparent so the
investment team can audit and tune them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .optimize import trim_to_risk_share

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# Liquid sector ETFs used to propose sector hedges in what-if links.
SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE", "Healthcare": "XLV",
    "Consumer Defensive": "XLP", "Industrials": "XLI", "Utilities": "XLU", "Consumer Cyclical": "XLY",
    "Communication Services": "XLC", "Basic Materials": "XLB", "Real Estate": "XLRE",
}


@dataclass
class Insight:
    severity: str        # high | medium | low | info
    category: str        # limits | concentration | factor | hedge | liquidity | tail | diversification | correlation
    title: str
    detail: str
    action: str = ""
    value: float | None = None
    threshold: float | None = None
    tickers: list[str] = field(default_factory=list)
    whatif: dict | None = None   # {"ticker", "mode", "value"} -> pre-fills the what-if page

    def as_dict(self):
        return self.__dict__.copy()


def _pct(x, digits=1):
    return f"{x * 100:.{digits}f}%"


def _money(x):
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e9:
        return f"{sign}${x / 1e9:.2f}bn"
    if x >= 1e6:
        return f"{sign}${x / 1e6:.2f}m"
    if x >= 1e3:
        return f"{sign}${x / 1e3:.0f}k"
    return f"{sign}${x:.0f}"


def generate_insights(ctx: dict) -> list[Insight]:
    """ctx keys (all optional except summary/rc/cov):
    summary: dict from PortfolioAnalyzer.summary()
    rc: risk contribution DataFrame (index ticker) with weight, pct, ccr
    sector_rc: DataFrame indexed by sector with weight, gross, pct
    cov: daily covariance DataFrame
    weights: Series
    factor: dict from factor_risk_decomposition (+ 'tstats' Series for the portfolio)
    limits: list of evaluated limit dicts (status, label, value, threshold)
    concentration: dict with hhi, effective_n, top5, div_ratio, enb
    liquidity: DataFrame with days_to_liquidate, weight
    stress: list of dicts with name, total_pnl (fraction of NAV)
    clusters: list from pairwise_clusters
    perf: performance stats dict
    hedge: dict with beta, benchmark, hedge_ticker, hedge_weight
    """
    out: list[Insight] = []
    s = ctx.get("summary", {})
    nav = s.get("nav", 0.0) or 0.0
    rc: pd.DataFrame | None = ctx.get("rc")
    cov = ctx.get("cov")
    weights: pd.Series | None = ctx.get("weights")

    # 1. Limit breaches and near-breaches ------------------------------------------------
    for lim in ctx.get("limits", []) or []:
        if lim.get("status") == "breach":
            whatif = None
            action = lim.get("action", "Reduce the exposure back inside the limit or seek an approved exception.")
            if lim.get("metric") == "position_weight" and rc is not None and len(rc):
                scope = (lim.get("scope") or "").strip()
                if scope in rc.index:
                    t = scope
                elif scope.lower() in ("equity", "stocks", "single_names", ""):
                    sub = rc[rc["asset_class"] == "Equity"] if scope and "asset_class" in rc.columns else rc
                    t = str(sub["weight"].abs().idxmax()) if len(sub) else None
                else:
                    t = None
                if t is not None:
                    w = float(rc.loc[t, "weight"])
                    target = float(lim["threshold"]) * 0.98 * (1 if w > 0 else -1)
                    whatif = {"ticker": t, "mode": "target_weight", "value": round(target, 4)}
                    action = (f"Cut {t} from {_pct(w)} to {_pct(target)} of NAV ({_money(abs(w - target) * nav)}) "
                              "to get back inside the limit.")
            elif lim.get("metric") == "max_risk_share" and rc is not None and len(rc) and cov is not None and weights is not None:
                t = str(rc["pct"].idxmax())
                w = float(rc.loc[t, "weight"])
                new_w = trim_to_risk_share(weights, cov, t, float(lim["threshold"]) * 0.95)
                if new_w is not None and abs(new_w) < abs(w):
                    whatif = {"ticker": t, "mode": "target_weight", "value": round(float(new_w), 4)}
                    action = (f"Trim {t} from {_pct(w)} to about {_pct(new_w)} of NAV ({_money(abs(w - new_w) * nav)}) "
                              f"to bring its risk share under {_pct(float(lim['threshold']), 0)}.")
            out.append(Insight("high", "limits", f"Limit breach: {lim['label']}",
                               f"Current {lim['display_value']} versus limit {lim['display_threshold']}"
                               f" ({lim.get('utilization', 0) * 100:.0f}% of limit).",
                               action, lim.get("value"), lim.get("threshold"), [t] if whatif else [], whatif))
        elif lim.get("status") == "warn":
            out.append(Insight("medium", "limits", f"Near limit: {lim['label']}",
                               f"Current {lim['display_value']} is at {lim.get('utilization', 0) * 100:.0f}% of the"
                               f" {lim['display_threshold']} limit.",
                               "No action required yet; avoid adding in this direction.",
                               lim.get("value"), lim.get("threshold")))

    # 2. Risk hogs: risk share far above weight share -------------------------------------
    if rc is not None and len(rc) and cov is not None and weights is not None:
        port_vol = rc.attrs.get("portfolio_vol", float("nan"))
        gross = rc["weight"].abs().sum()
        for t, row in rc.sort_values("pct", ascending=False).head(3).iterrows():
            wshare = abs(row["weight"]) / gross if gross else 0
            if row["pct"] >= 0.15 and row["pct"] >= 1.8 * wshare:
                target = max(row["pct"] / 2, 0.08)
                new_w = trim_to_risk_share(weights, cov, t, target)
                action, whatif = "", None
                if new_w is not None and abs(new_w) < abs(row["weight"]):
                    action = (f"Trim {t} from {_pct(row['weight'])} to about {_pct(new_w)} of NAV"
                              f" ({_money((row['weight'] - new_w) * nav)}) to cut its risk share to ~{_pct(target, 0)}.")
                    whatif = {"ticker": t, "mode": "target_weight", "value": round(float(new_w), 4)}
                out.append(Insight("medium", "concentration", f"{t} is a risk hog",
                                   f"{t} is {_pct(row['weight'])} of NAV but contributes {_pct(row['pct'])} of portfolio"
                                   f" volatility ({row['ccr'] * 100:.1f} vol points of {port_vol * 100:.1f}%)."
                                   f" Its standalone vol is {_pct(row['standalone_vol'], 0)}.",
                                   action, float(row["pct"]), None, [t], whatif))
        # 2b. Negative contributors (hedges) that are not actually hedging
        for t, row in rc.iterrows():
            if row["weight"] < 0 and row["pct"] > 0.03:
                out.append(Insight("medium", "hedge", f"Short {t} is adding risk, not hedging",
                                   f"The {_pct(abs(row['weight']))} short in {t} has a positive risk contribution"
                                   f" ({_pct(row['pct'])} of vol), meaning it moves against the rest of the book"
                                   " less than expected.",
                                   "Check whether this short is meant as alpha or as a hedge; if a hedge, replace it with"
                                   " an instrument more negatively correlated to the long book.", float(row["pct"]), None, [t]))

    # 3. Sector concentration --------------------------------------------------------------
    sector_rc = ctx.get("sector_rc")
    if sector_rc is not None and len(sector_rc):
        top = sector_rc.sort_values("pct", ascending=False).iloc[0]
        if top["pct"] > 0.45:
            etf = SECTOR_ETF.get(str(top.name))
            hedge_w = float(top["weight"]) * 0.3
            whatif = {"ticker": etf, "mode": "delta_weight", "value": round(-hedge_w, 4)} if etf and hedge_w > 0 else None
            out.append(Insight("medium", "concentration", f"{top.name} drives {_pct(top['pct'], 0)} of risk",
                               f"{top.name} is {_pct(top['gross'], 0)} gross exposure but {_pct(top['pct'], 0)} of portfolio vol.",
                               f"Diversify within {top.name} or add a sector hedge ({'short ' + etf if etf else 'the sector ETF'}) sized to"
                               f" roughly {_pct(abs(hedge_w), 0)} of NAV to bring its risk share below 35%.",
                               float(top["pct"]), 0.45, [], whatif))

    # 4. Factor tilts ---------------------------------------------------------------------
    fac = ctx.get("factor")
    if fac and "exposures" in fac:
        ex = fac["exposures"]
        for f, row in ex.iterrows():
            if f == "MKT":
                continue
            share = row["pct_of_total_var"]
            if abs(row["beta"]) > 0.25 and share > 0.08:
                direction = "long" if row["beta"] > 0 else "short"
                out.append(Insight("low", "factor", f"Unintended {direction} {f} tilt",
                                   f"Portfolio beta to {f} is {row['beta']:+.2f}, explaining {_pct(share, 0)} of total"
                                   f" variance ({row['factor_vol'] * 100:.0f}% factor vol).",
                                   f"If the {f} tilt is not a deliberate view, neutralise it with the factor ETF pair"
                                   " or by rebalancing across names with opposite loadings.", float(row["beta"]), 0.25))
        if fac.get("systematic_share", 0) > 0.85:
            out.append(Insight("info", "factor", "Book is mostly systematic risk",
                               f"{_pct(fac['systematic_share'], 0)} of variance is explained by factors; stock selection"
                               f" (idiosyncratic) risk is only {_pct(fac['idiosyncratic_vol'], 1)} of the {_pct(fac['total_vol'], 1)} total.",
                               "If the mandate is stock-picking alpha, hedge factor exposure to let idiosyncratic risk dominate."))
        elif 0 < fac.get("systematic_share", 1) < 0.4:
            out.append(Insight("info", "factor", "Risk is mostly idiosyncratic",
                               f"Only {_pct(fac['systematic_share'], 0)} of variance is factor-driven; the book is a"
                               " genuine stock-picking portfolio.", ""))

    # 5. Hedge sizing ----------------------------------------------------------------------
    hedge = ctx.get("hedge")
    if hedge and hedge.get("beta") is not None and not np.isnan(hedge["beta"]):
        beta = hedge["beta"]
        target = hedge.get("target_beta")
        if target is not None and abs(beta - target) > 0.15:
            delta_w = -(beta - target)  # benchmark weight change needed
            bench = hedge.get("benchmark", "SPY")
            out.append(Insight("medium", "hedge", f"Beta {beta:+.2f} vs target {target:+.2f}",
                               f"Portfolio beta to {bench} is {beta:+.2f}.",
                               f"{'Buy' if delta_w > 0 else 'Short'} {bench} for about {_pct(abs(delta_w))} of NAV"
                               f" ({_money(abs(delta_w) * nav)}) to reach the target beta.", float(beta), target, [bench],
                               {"ticker": bench, "mode": "delta_weight", "value": round(float(delta_w), 4)}))

    # 6. Correlated pairs --------------------------------------------------------------------
    for pair in (ctx.get("clusters") or [])[:3]:
        if pair["stacked"]:
            out.append(Insight("low", "correlation", f"{pair['a']} and {pair['b']} are one bet",
                               f"Correlation {pair['corr']:+.2f} with combined gross {_pct(pair['combined_gross'])} of NAV.",
                               "Treat them as a single position for sizing; consider consolidating into the higher-conviction name.",
                               pair["corr"], 0.75, [pair["a"], pair["b"]]))

    # 7. Concentration stats ----------------------------------------------------------------
    conc = ctx.get("concentration") or {}
    if conc.get("top5") and conc["top5"] > 0.5:
        out.append(Insight("low", "concentration", f"Top 5 positions are {_pct(conc['top5'], 0)} of gross",
                           f"Effective number of positions is {conc.get('effective_n', float('nan')):.1f} versus"
                           f" {conc.get('n_positions', 0)} held.", "Spread the tail of the book or accept the concentration explicitly."))
    if conc.get("enb") and conc.get("n_positions", 0) >= 8 and conc["enb"] < 3:
        out.append(Insight("low", "diversification", f"Only {conc['enb']:.1f} independent bets",
                           "Effective number of bets (entropy of principal-portfolio variance shares) is low;"
                           " most risk sits in one or two common factors.",
                           "Add exposures that load on different principal components (rates, commodities, low-beta names)."))

    # 8. Liquidity ---------------------------------------------------------------------------
    liq = ctx.get("liquidity")
    if liq is not None and len(liq):
        slow = liq[liq["days_to_liquidate"] > 5]
        if len(slow):
            names = ", ".join(f"{t} ({d:.0f}d)" for t, d in slow["days_to_liquidate"].head(4).items())
            out.append(Insight("medium" if slow["weight"].abs().sum() > 0.1 else "low", "liquidity",
                               f"{len(slow)} position(s) need >5 days to exit",
                               f"At 20% of ADV: {names}. Combined {_pct(slow['weight'].abs().sum())} of NAV.",
                               "Cap illiquid names at a size that liquidates within 3 days, or pre-plan staged exits.",
                               float(slow["days_to_liquidate"].max()), 5.0, list(slow.index[:4])))

    # 9. Stress -------------------------------------------------------------------------------
    stress = ctx.get("stress") or []
    if stress:
        worst = min(stress, key=lambda d: d["total_pnl"])
        if worst["total_pnl"] < -0.10:
            out.append(Insight("medium", "tail", f"Worst scenario: {worst['name']} {_pct(worst['total_pnl'])}",
                               f"Replaying {worst['name']} on today's book loses {_money(worst['total_pnl'] * nav)}"
                               f" ({_pct(worst['total_pnl'])} of NAV).",
                               "Compare with the risk budget; if too large, the cheapest fix is usually reducing the"
                               " largest contributors to that scenario rather than the whole book.",
                               worst["total_pnl"], -0.10))

    # 10. Tail shape ---------------------------------------------------------------------------
    perf = ctx.get("perf") or {}
    if perf.get("kurtosis", 0) > 5:
        out.append(Insight("low", "tail", "Fat-tailed return distribution",
                           f"Excess kurtosis {perf['kurtosis']:.1f} and skew {perf.get('skew', 0):+.2f}. Parametric VaR"
                           " will understate tail losses.", "Use historical or Cornish-Fisher VaR for limits and sizing."))
    if perf.get("current_drawdown", 0) < -0.08:
        out.append(Insight("medium", "tail", f"Book is {_pct(perf['current_drawdown'])} below its high-water mark",
                           "Pro-forma drawdown of the current holdings.", "Consider vol-scaling down until the drawdown recovers."))

    out.sort(key=lambda i: SEVERITY_ORDER.get(i.severity, 9))
    return out
