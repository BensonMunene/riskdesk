"""Core portfolio risk measures: volatility, contributions, VaR/CVaR, drawdowns, betas."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from .returns import TRADING_DAYS, annualize_return, annualize_vol


# ----------------------------------------------------------------------------
# Volatility and risk contributions
# ----------------------------------------------------------------------------

def portfolio_variance(w: np.ndarray, cov: np.ndarray) -> float:
    return float(w @ cov @ w)


def portfolio_vol(w: np.ndarray, cov: np.ndarray, periods: int = TRADING_DAYS) -> float:
    """Annualised portfolio volatility from *daily* covariance."""
    return float(np.sqrt(max(portfolio_variance(w, cov), 0.0) * periods))


def risk_contributions(weights: pd.Series, cov: pd.DataFrame, periods: int = TRADING_DAYS) -> pd.DataFrame:
    """Euler decomposition of portfolio volatility.

    Returns a DataFrame indexed by asset with columns:
      weight, mcr (marginal contribution d(sigma)/d(w), annualised),
      ccr (component contribution in vol units, annualised; sums to portfolio vol),
      pct (share of total vol, sums to 1),
      standalone_vol (annualised asset vol),
      beta_to_port (asset beta to the portfolio).
    """
    w = weights.reindex(cov.index).fillna(0.0)
    wv = w.to_numpy(dtype=float)
    c = cov.to_numpy(dtype=float)
    var = float(wv @ c @ wv)
    sigma = np.sqrt(max(var, 1e-18))
    sigma_w = c @ wv
    mcr = sigma_w / sigma
    ccr = wv * mcr
    pct = ccr / sigma if sigma > 0 else np.zeros_like(ccr)
    standalone = np.sqrt(np.diag(c))
    beta = sigma_w / var if var > 0 else np.zeros_like(ccr)
    out = pd.DataFrame({
        "weight": wv,
        "mcr": mcr * np.sqrt(periods),
        "ccr": ccr * np.sqrt(periods),
        "pct": pct,
        "standalone_vol": standalone * np.sqrt(periods),
        "beta_to_port": beta,
    }, index=cov.index)
    out.attrs["portfolio_vol"] = float(sigma * np.sqrt(periods))
    return out


def group_risk_contributions(rc: pd.DataFrame, groups: pd.Series) -> pd.DataFrame:
    """Aggregate component contributions by a grouping (e.g. sector)."""
    g = groups.reindex(rc.index).fillna("Unclassified")
    agg = rc.groupby(g).agg(
        weight=("weight", "sum"),
        gross=("weight", lambda s: s.abs().sum()),
        ccr=("ccr", "sum"),
        pct=("pct", "sum"),
        n=("weight", "size"),
    )
    return agg.sort_values("pct", ascending=False)


# ----------------------------------------------------------------------------
# Value at Risk
# ----------------------------------------------------------------------------

@dataclass
class VaRResult:
    method: str
    confidence: float
    horizon_days: int
    var: float      # positive number = loss as fraction of NAV
    cvar: float

    def as_dict(self):
        return {"method": self.method, "confidence": self.confidence,
                "horizon_days": self.horizon_days, "var": self.var, "cvar": self.cvar}


def historical_var(returns: pd.Series, confidence: float = 0.99, horizon_days: int = 1) -> VaRResult:
    r = returns.dropna().to_numpy(dtype=float)
    if len(r) == 0:
        return VaRResult("historical", confidence, horizon_days, float("nan"), float("nan"))
    q = np.quantile(r, 1.0 - confidence)
    tail = r[r <= q]
    cvar = tail.mean() if len(tail) else q
    scale = np.sqrt(horizon_days)
    return VaRResult("historical", confidence, horizon_days, float(-q * scale), float(-cvar * scale))


def parametric_var(returns: pd.Series, confidence: float = 0.99, horizon_days: int = 1,
                   sigma: float | None = None, mu: float | None = None) -> VaRResult:
    r = returns.dropna().to_numpy(dtype=float)
    mu = float(r.mean()) if mu is None else mu
    sigma = float(r.std(ddof=1)) if sigma is None else sigma
    z = stats.norm.ppf(1.0 - confidence)
    var = -(mu + z * sigma)
    cvar = -(mu - sigma * stats.norm.pdf(z) / (1.0 - confidence))
    scale = np.sqrt(horizon_days)
    return VaRResult("parametric", confidence, horizon_days, float(var * scale), float(cvar * scale))


def cornish_fisher_var(returns: pd.Series, confidence: float = 0.99, horizon_days: int = 1) -> VaRResult:
    """Parametric VaR adjusted for skew and excess kurtosis (Cornish-Fisher expansion)."""
    r = returns.dropna().to_numpy(dtype=float)
    mu, sigma = float(r.mean()), float(r.std(ddof=1))
    s, k = float(stats.skew(r)), float(stats.kurtosis(r))
    z = stats.norm.ppf(1.0 - confidence)
    z_cf = z + (z ** 2 - 1) * s / 6 + (z ** 3 - 3 * z) * k / 24 - (2 * z ** 3 - 5 * z) * s ** 2 / 36
    var = -(mu + z_cf * sigma)
    alphas = np.linspace(1e-4, 1.0 - confidence, 50)
    zs = stats.norm.ppf(alphas)
    zs_cf = zs + (zs ** 2 - 1) * s / 6 + (zs ** 3 - 3 * zs) * k / 24 - (2 * zs ** 3 - 5 * zs) * s ** 2 / 36
    cvar = -(mu + sigma * zs_cf.mean())
    scale = np.sqrt(horizon_days)
    return VaRResult("cornish_fisher", confidence, horizon_days, float(var * scale), float(cvar * scale))


def monte_carlo_var(weights: np.ndarray, cov: np.ndarray, mu: np.ndarray | None = None,
                    confidence: float = 0.99, horizon_days: int = 1, n_sims: int = 20000,
                    seed: int = 42, df: float | None = 5.0) -> VaRResult:
    """Monte Carlo VaR from a multivariate normal or Student-t (fat-tailed) simulation."""
    rng = np.random.default_rng(seed)
    n = len(weights)
    mu = np.zeros(n) if mu is None else mu
    L = np.linalg.cholesky(cov + np.eye(n) * 1e-12)
    z = rng.standard_normal((n_sims, n))
    if df is not None and df > 2:
        chi = rng.chisquare(df, size=(n_sims, 1)) / df
        z = z / np.sqrt(chi) * np.sqrt((df - 2) / df)  # unit-variance Student-t
    sims = mu + z @ L.T
    port = sims @ weights * np.sqrt(horizon_days)
    q = np.quantile(port, 1.0 - confidence)
    cvar = port[port <= q].mean()
    return VaRResult("monte_carlo", confidence, horizon_days, float(-q), float(-cvar))


def var_suite(port_returns: pd.Series, weights: np.ndarray, cov: np.ndarray,
              confidences=(0.95, 0.99), horizon_days: int = 1) -> list[VaRResult]:
    out = []
    for c in confidences:
        out.append(historical_var(port_returns, c, horizon_days))
        out.append(parametric_var(port_returns, c, horizon_days))
        out.append(cornish_fisher_var(port_returns, c, horizon_days))
        out.append(monte_carlo_var(weights, cov, confidence=c, horizon_days=horizon_days))
    return out


def var_contributions(weights: pd.Series, returns: pd.DataFrame, confidence: float = 0.99) -> pd.DataFrame:
    """Component historical CVaR: average asset P&L (as % NAV) on the tail days."""
    w = weights.reindex(returns.columns).fillna(0.0)
    port = returns.fillna(0.0) @ w
    q = port.quantile(1.0 - confidence)
    tail_days = port[port <= q].index
    contrib = (returns.loc[tail_days].fillna(0.0) * w).mean()
    total = contrib.sum()
    pct = contrib / total if total != 0 else contrib * 0
    return pd.DataFrame({"weight": w, "cvar_contrib": -contrib, "pct": pct}).sort_values("cvar_contrib", ascending=False)


# ----------------------------------------------------------------------------
# Drawdowns and performance
# ----------------------------------------------------------------------------

def drawdown_series(daily: pd.Series) -> pd.Series:
    wealth = (1.0 + daily.fillna(0.0)).cumprod()
    peak = wealth.cummax()
    return wealth / peak - 1.0


def drawdown_table(daily: pd.Series, top: int = 5) -> pd.DataFrame:
    dd = drawdown_series(daily)
    in_dd = dd < 0
    episodes = []
    start = None
    for date, flag in in_dd.items():
        if flag and start is None:
            start = date
        elif not flag and start is not None:
            seg = dd.loc[start:date]
            episodes.append((start, seg.idxmin(), date, float(seg.min()), len(seg)))
            start = None
    if start is not None:
        seg = dd.loc[start:]
        episodes.append((start, seg.idxmin(), None, float(seg.min()), len(seg)))
    df = pd.DataFrame(episodes, columns=["start", "trough", "recovery", "depth", "length_days"])
    df = df.sort_values("depth").head(top).reset_index(drop=True)
    # keep an ongoing episode's recovery as a real None (not NaT) so templates can test it safely
    df["recovery"] = df["recovery"].astype(object).where(df["recovery"].notna(), None)
    return df


def performance_stats(daily: pd.Series, rf: float = 0.0, periods: int = TRADING_DAYS,
                      benchmark: pd.Series | None = None) -> dict:
    d = daily.dropna()
    if len(d) < 2:
        return {}
    ann_ret = annualize_return(d, periods)
    ann_vol = annualize_vol(d, periods)
    downside = d[d < 0].std(ddof=1) * np.sqrt(periods) if (d < 0).sum() > 1 else float("nan")
    dd = drawdown_series(d)
    max_dd = float(dd.min())
    out = {
        "n_days": int(len(d)),
        "start": d.index[0],
        "end": d.index[-1],
        "total_return": float((1 + d).prod() - 1),
        "ann_return": ann_ret,
        "ann_vol": ann_vol,
        "sharpe": (ann_ret - rf) / ann_vol if ann_vol > 0 else float("nan"),
        "sortino": (ann_ret - rf) / downside if downside and downside > 0 else float("nan"),
        "max_drawdown": max_dd,
        "calmar": ann_ret / abs(max_dd) if max_dd < 0 else float("nan"),
        "current_drawdown": float(dd.iloc[-1]),
        "skew": float(stats.skew(d)),
        "kurtosis": float(stats.kurtosis(d)),
        "best_day": float(d.max()),
        "worst_day": float(d.min()),
        "best_day_date": d.idxmax(),
        "worst_day_date": d.idxmin(),
        "hit_rate": float((d > 0).mean()),
        "avg_up": float(d[d > 0].mean()) if (d > 0).any() else 0.0,
        "avg_down": float(d[d < 0].mean()) if (d < 0).any() else 0.0,
    }
    ytd = d[d.index >= pd.Timestamp(d.index[-1].year, 1, 1)]
    out["ytd_return"] = float((1 + ytd).prod() - 1) if len(ytd) else float("nan")
    for label, n in (("1m", 21), ("3m", 63), ("6m", 126), ("1y", 252)):
        seg = d.iloc[-n:]
        out[f"ret_{label}"] = float((1 + seg).prod() - 1) if len(seg) >= min(n, 5) else float("nan")
    if benchmark is not None:
        b = benchmark.reindex(d.index).dropna()
        p = d.reindex(b.index)
        if len(b) > 10:
            cov = np.cov(p, b, ddof=1)
            beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else float("nan")
            active = p - b
            te = float(active.std(ddof=1) * np.sqrt(periods))
            out.update({
                "beta": float(beta),
                "alpha_ann": float((p.mean() - beta * b.mean()) * periods),
                "correlation": float(np.corrcoef(p, b)[0, 1]),
                "tracking_error": te,
                "information_ratio": float(active.mean() * periods / te) if te > 0 else float("nan"),
                "benchmark_ann_return": annualize_return(b, periods),
                "benchmark_ann_vol": annualize_vol(b, periods),
                "benchmark_total_return": float((1 + b).prod() - 1),
                "up_capture": _capture(p, b, up=True),
                "down_capture": _capture(p, b, up=False),
            })
    return out


def _capture(p: pd.Series, b: pd.Series, up: bool) -> float:
    mask = b > 0 if up else b < 0
    if mask.sum() < 5 or b[mask].mean() == 0:
        return float("nan")
    return float(p[mask].mean() / b[mask].mean())


def rolling_vol(daily: pd.Series, window: int = 21, periods: int = TRADING_DAYS) -> pd.Series:
    return (daily.rolling(window).std(ddof=1) * np.sqrt(periods)).dropna()


def rolling_beta(daily: pd.Series, bench: pd.Series, window: int = 63) -> pd.Series:
    df = pd.concat([daily, bench], axis=1, keys=["p", "b"]).dropna()
    cov = df["p"].rolling(window).cov(df["b"])
    var = df["b"].rolling(window).var()
    return (cov / var).dropna()


def beta_to(asset_returns: pd.DataFrame, bench: pd.Series) -> pd.Series:
    """Per-asset beta to a benchmark series."""
    b = bench.reindex(asset_returns.index)
    var_b = b.var(ddof=1)
    if var_b == 0 or np.isnan(var_b):
        return pd.Series(np.nan, index=asset_returns.columns)
    return asset_returns.apply(lambda col: col.cov(b) / var_b)
