"""Covariance estimators.

All estimators take a DataFrame of *daily* returns (rows = dates, columns = assets)
and return a daily covariance DataFrame. Annualise by multiplying by 252.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .returns import TRADING_DAYS

COV_METHODS = {
    "sample": "Sample covariance",
    "ewma": "Exponentially weighted (RiskMetrics style)",
    "ledoit_wolf": "Ledoit-Wolf constant-correlation shrinkage",
}


def sample_cov(returns: pd.DataFrame) -> pd.DataFrame:
    return returns.cov(ddof=1)


def ewma_cov(returns: pd.DataFrame, halflife: int = 60) -> pd.DataFrame:
    """EWMA covariance with the given half-life in trading days."""
    lam = 0.5 ** (1.0 / max(halflife, 1))
    x = returns.fillna(0.0).to_numpy(dtype=float)
    n = x.shape[0]
    w = lam ** np.arange(n - 1, -1, -1)
    w /= w.sum()
    mean = (w[:, None] * x).sum(axis=0)
    xc = x - mean
    cov = (xc * w[:, None]).T @ xc
    return pd.DataFrame(cov, index=returns.columns, columns=returns.columns)


def ledoit_wolf_cov(returns: pd.DataFrame) -> pd.DataFrame:
    """Ledoit & Wolf (2004) shrinkage toward the constant-correlation target.

    Implementation follows "Honey, I Shrunk the Sample Covariance Matrix".
    Returns the shrunk daily covariance matrix; the shrinkage intensity is
    stored in ``result.attrs["shrinkage"]``.
    """
    x = returns.dropna(how="any").to_numpy(dtype=float)
    t, n = x.shape
    if t < 3 or n == 1:
        return sample_cov(returns)
    x = x - x.mean(axis=0)
    sample = (x.T @ x) / t
    var = np.diag(sample).copy()
    sqrt_var = np.sqrt(var)
    corr = sample / np.outer(sqrt_var, sqrt_var)
    rbar = (corr.sum() - n) / (n * (n - 1))
    target = rbar * np.outer(sqrt_var, sqrt_var)
    np.fill_diagonal(target, var)

    # pi hat: sum of asymptotic variances of the sample covariance entries
    y = x ** 2
    pi_mat = (y.T @ y) / t - sample ** 2
    pi_hat = pi_mat.sum()

    # rho hat: asymptotic covariance between sample and target entries
    term1 = ((x ** 3).T @ x) / t
    help_ = np.outer(var, np.ones(n))
    theta = term1 - help_ * sample
    np.fill_diagonal(theta, 0.0)
    rho_hat = np.diag(pi_mat).sum() + rbar * ((np.outer(1.0 / sqrt_var, sqrt_var) * theta).sum())

    gamma_hat = np.linalg.norm(sample - target, "fro") ** 2
    kappa = (pi_hat - rho_hat) / gamma_hat if gamma_hat > 0 else 0.0
    shrink = float(np.clip(kappa / t, 0.0, 1.0))
    sigma = shrink * target + (1.0 - shrink) * sample
    sigma = pd.DataFrame(sigma, index=returns.columns, columns=returns.columns)
    sigma.attrs["shrinkage"] = shrink
    return sigma


def estimate_cov(returns: pd.DataFrame, method: str = "ledoit_wolf", halflife: int = 60) -> pd.DataFrame:
    method = (method or "ledoit_wolf").lower()
    if method == "sample":
        return sample_cov(returns)
    if method == "ewma":
        return ewma_cov(returns, halflife=halflife)
    if method == "ledoit_wolf":
        return ledoit_wolf_cov(returns)
    raise ValueError(f"Unknown covariance method: {method}")


def cov_to_corr(cov: pd.DataFrame) -> pd.DataFrame:
    sd = np.sqrt(np.diag(cov.to_numpy()))
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = cov.to_numpy() / np.outer(sd, sd)
    corr = np.nan_to_num(corr, nan=0.0)
    np.fill_diagonal(corr, 1.0)
    return pd.DataFrame(corr, index=cov.index, columns=cov.columns)


def annualize_cov(cov: pd.DataFrame, periods: int = TRADING_DAYS) -> pd.DataFrame:
    return cov * periods


def nearest_psd(cov: pd.DataFrame, eps: float = 1e-10) -> pd.DataFrame:
    """Clip negative eigenvalues so optimisers receive a PSD matrix."""
    a = (cov.to_numpy() + cov.to_numpy().T) / 2.0
    vals, vecs = np.linalg.eigh(a)
    vals = np.clip(vals, eps, None)
    fixed = (vecs * vals) @ vecs.T
    return pd.DataFrame(fixed, index=cov.index, columns=cov.columns)
