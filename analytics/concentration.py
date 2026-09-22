"""Concentration and diversification statistics."""
from __future__ import annotations

import numpy as np
import pandas as pd


def hhi(weights: pd.Series, gross: bool = True) -> float:
    """Herfindahl-Hirschman index on gross (abs) weights normalised to sum to 1."""
    w = weights.abs() if gross else weights.clip(lower=0)
    s = w.sum()
    if s == 0:
        return float("nan")
    w = w / s
    return float((w ** 2).sum())


def effective_n(weights: pd.Series) -> float:
    h = hhi(weights)
    return 1.0 / h if h and h > 0 else float("nan")


def top_n_weight(weights: pd.Series, n: int = 5) -> float:
    w = weights.abs()
    s = w.sum()
    if s == 0:
        return float("nan")
    return float(w.sort_values(ascending=False).head(n).sum() / s)


def diversification_ratio(weights: pd.Series, cov: pd.DataFrame) -> float:
    """Choueifaty-Coignard ratio: weighted average vol / portfolio vol (gross weights)."""
    w = weights.reindex(cov.index).fillna(0.0).to_numpy(dtype=float)
    c = cov.to_numpy(dtype=float)
    sd = np.sqrt(np.diag(c))
    pv = np.sqrt(max(w @ c @ w, 1e-18))
    return float((np.abs(w) @ sd) / pv) if pv > 0 else float("nan")


def effective_number_of_bets(weights: pd.Series, cov: pd.DataFrame) -> float:
    """Meucci (2009) effective number of bets via principal portfolios.

    Uses the entropy of the variance shares along the principal components.
    """
    w = weights.reindex(cov.index).fillna(0.0).to_numpy(dtype=float)
    c = cov.to_numpy(dtype=float)
    vals, vecs = np.linalg.eigh(c)
    vals = np.clip(vals, 1e-16, None)
    loadings = vecs.T @ w
    var_shares = (loadings ** 2) * vals
    total = var_shares.sum()
    if total <= 0:
        return float("nan")
    p = var_shares / total
    p = p[p > 0]
    entropy = -(p * np.log(p)).sum()
    return float(np.exp(entropy))


def group_weights(weights: pd.Series, groups: pd.Series) -> pd.DataFrame:
    g = groups.reindex(weights.index).fillna("Unclassified")
    df = pd.DataFrame({"weight": weights, "group": g})
    out = df.groupby("group")["weight"].agg(
        net="sum",
        gross=lambda s: s.abs().sum(),
        long=lambda s: s[s > 0].sum(),
        short=lambda s: s[s < 0].sum(),
        n="size",
    )
    return out.sort_values("gross", ascending=False)


def pairwise_clusters(corr: pd.DataFrame, weights: pd.Series, threshold: float = 0.75) -> list[dict]:
    """Highly correlated pairs held in the same direction (effectively one bet)."""
    out = []
    tickers = list(corr.index)
    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            a, b = tickers[i], tickers[j]
            rho = float(corr.iloc[i, j])
            wa, wb = float(weights.get(a, 0.0)), float(weights.get(b, 0.0))
            if abs(rho) >= threshold and wa * wb != 0:
                same_direction = (wa > 0) == (wb > 0)
                # positive corr + same direction => stacked bet; negative corr + opposite => also stacked
                stacked = (rho > 0 and same_direction) or (rho < 0 and not same_direction)
                out.append({"a": a, "b": b, "corr": rho, "weight_a": wa, "weight_b": wb,
                            "stacked": stacked, "combined_gross": abs(wa) + abs(wb)})
    out.sort(key=lambda d: -abs(d["corr"]))
    return out
