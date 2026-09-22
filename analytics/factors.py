"""ETF-based multi-factor risk model.

Factor returns are built from liquid ETFs so the model works with nothing but
price data (no vendor factor library required):

  MKT     SPY                       broad US equity
  SIZE    IWM - SPY                 small minus large
  VALUE   IWD - IWF                 value minus growth
  MOM     MTUM - SPY                momentum tilt
  QUALITY QUAL - SPY                quality tilt
  LOWVOL  USMV - SPY                low-volatility tilt
  RATES   TLT                       long-duration Treasuries (price, so +ve = yields down)
  CREDIT  HYG - IEF                 high-yield credit spread proxy
  USD     UUP                       US dollar
  GOLD    GLD                       gold
  OIL     USO                       crude oil

Each asset is regressed on the factor returns (OLS) over the lookback window.
Portfolio factor exposures are the weight-averaged betas. Portfolio variance is
split into systematic (factor) and idiosyncratic (residual) parts.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .returns import TRADING_DAYS

FACTOR_SPEC: dict[str, tuple[str, str | None]] = {
    "MKT": ("SPY", None),
    "SIZE": ("IWM", "SPY"),
    "VALUE": ("IWD", "IWF"),
    "MOM": ("MTUM", "SPY"),
    "QUALITY": ("QUAL", "SPY"),
    "LOWVOL": ("USMV", "SPY"),
    "RATES": ("TLT", None),
    "CREDIT": ("HYG", "IEF"),
    "USD": ("UUP", None),
    "GOLD": ("GLD", None),
    "OIL": ("USO", None),
}

FACTOR_DESCRIPTIONS = {
    "MKT": "Broad US equity market (SPY)",
    "SIZE": "Small cap minus large cap (IWM - SPY)",
    "VALUE": "Value minus growth (IWD - IWF)",
    "MOM": "Momentum minus market (MTUM - SPY)",
    "QUALITY": "Quality minus market (QUAL - SPY)",
    "LOWVOL": "Low volatility minus market (USMV - SPY)",
    "RATES": "Long Treasury price (TLT); positive beta = benefits from falling yields",
    "CREDIT": "High yield minus Treasuries (HYG - IEF), a credit-spread proxy",
    "USD": "US dollar index (UUP)",
    "GOLD": "Gold (GLD)",
    "OIL": "Crude oil (USO)",
}


def required_tickers() -> list[str]:
    out: list[str] = []
    for long, short in FACTOR_SPEC.values():
        for t in (long, short):
            if t and t not in out:
                out.append(t)
    return out


def build_factor_returns(etf_returns: pd.DataFrame, spec: dict | None = None) -> pd.DataFrame:
    spec = spec or FACTOR_SPEC
    cols = {}
    for name, (long, short) in spec.items():
        if long not in etf_returns.columns:
            continue
        series = etf_returns[long]
        if short:
            if short not in etf_returns.columns:
                continue
            series = series - etf_returns[short]
        cols[name] = series
    return pd.DataFrame(cols).dropna(how="all")


def factor_regression(asset_returns: pd.DataFrame, factor_returns: pd.DataFrame,
                      periods: int = TRADING_DAYS) -> dict:
    """OLS of each asset on the factors (with intercept).

    Returns dict with:
      betas: DataFrame assets x factors
      alpha: Series (annualised)
      r2: Series
      resid_vol: Series (annualised idiosyncratic vol)
      tstats: DataFrame assets x factors
      n_obs: int
    """
    common = asset_returns.index.intersection(factor_returns.index)
    y = asset_returns.loc[common]
    f = factor_returns.loc[common]
    x = np.column_stack([np.ones(len(f)), f.to_numpy(dtype=float)])
    yv = y.fillna(0.0).to_numpy(dtype=float)
    n, k = x.shape
    coef, *_ = np.linalg.lstsq(x, yv, rcond=None)
    fitted = x @ coef
    resid = yv - fitted
    ss_res = (resid ** 2).sum(axis=0)
    ss_tot = ((yv - yv.mean(axis=0)) ** 2).sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        r2 = np.where(ss_tot > 0, 1.0 - ss_res / ss_tot, 0.0)
    dof = max(n - k, 1)
    sigma2 = ss_res / dof
    xtx_inv = np.linalg.pinv(x.T @ x)
    se = np.sqrt(np.outer(np.diag(xtx_inv), sigma2))  # k x assets
    with np.errstate(divide="ignore", invalid="ignore"):
        tstats = np.where(se > 0, coef / se, 0.0)
    betas = pd.DataFrame(coef[1:].T, index=y.columns, columns=f.columns)
    return {
        "betas": betas,
        "alpha": pd.Series(coef[0] * periods, index=y.columns),
        "r2": pd.Series(r2, index=y.columns),
        "resid_vol": pd.Series(np.sqrt(sigma2 * periods), index=y.columns),
        "resid_var_daily": pd.Series(sigma2, index=y.columns),
        "tstats": pd.DataFrame(tstats[1:].T, index=y.columns, columns=f.columns),
        "n_obs": int(n),
        "factor_returns": f,
        "residuals": pd.DataFrame(resid, index=common, columns=y.columns),
    }


def portfolio_factor_exposures(weights: pd.Series, betas: pd.DataFrame) -> pd.Series:
    w = weights.reindex(betas.index).fillna(0.0)
    return betas.mul(w, axis=0).sum(axis=0)


def factor_risk_decomposition(weights: pd.Series, reg: dict, periods: int = TRADING_DAYS) -> dict:
    """Split portfolio variance into factor and idiosyncratic components.

    Var(p) = b' F b + sum_i w_i^2 s_i^2  where b = portfolio factor betas,
    F = factor covariance, s_i = residual vol of asset i.
    """
    betas: pd.DataFrame = reg["betas"]
    f: pd.DataFrame = reg["factor_returns"]
    w = weights.reindex(betas.index).fillna(0.0)
    b = portfolio_factor_exposures(w, betas)
    fcov = f.cov(ddof=1)
    sys_var = float(b @ fcov @ b)
    idio_var = float((w ** 2 * reg["resid_var_daily"].reindex(w.index).fillna(0.0)).sum())
    total_var = sys_var + idio_var
    # per-factor contribution (Euler): b_k * (F b)_k
    contrib = b * (fcov @ b)
    contrib_pct = contrib / total_var if total_var > 0 else contrib * 0
    factor_vols = np.sqrt(np.diag(fcov)) * np.sqrt(periods)
    exposure_table = pd.DataFrame({
        "beta": b,
        "factor_vol": pd.Series(factor_vols, index=fcov.index),
        "var_contrib": contrib * periods,
        "pct_of_total_var": contrib_pct,
        "vol_contrib": np.sign(contrib) * np.sqrt(np.abs(contrib) * periods),
    })
    return {
        "exposures": exposure_table,
        "systematic_vol": float(np.sqrt(sys_var * periods)),
        "idiosyncratic_vol": float(np.sqrt(idio_var * periods)),
        "total_vol": float(np.sqrt(total_var * periods)),
        "systematic_share": sys_var / total_var if total_var > 0 else float("nan"),
        "factor_cov": fcov,
        "portfolio_r2": sys_var / total_var if total_var > 0 else float("nan"),
    }


def factor_implied_returns(betas: pd.DataFrame, factor_shocks: dict[str, float]) -> pd.Series:
    """Asset returns implied by a set of factor shocks (used by stress tests)."""
    shock = pd.Series(factor_shocks, dtype=float).reindex(betas.columns).fillna(0.0)
    return betas @ shock
