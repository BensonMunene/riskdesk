"""Portfolio construction: convex optimisers, Black-Litterman and position sizing.

All covariance inputs to this module are *annualised* (daily cov x 252) and all
expected returns are annualised, so volatilities and Sharpe ratios come out in
the usual units.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cvxpy as cp
import numpy as np
import pandas as pd
from scipy import optimize as sopt

from .covariance import nearest_psd
from .returns import TRADING_DAYS

METHODS = {
    "min_variance": "Minimum variance",
    "max_sharpe": "Maximum Sharpe ratio",
    "mean_variance": "Mean-variance (risk-aversion)",
    "target_return": "Minimum variance for a target return",
    "target_vol": "Maximum return for a target volatility",
    "risk_parity": "Risk parity / equal risk contribution",
    "max_diversification": "Maximum diversification ratio",
    "black_litterman": "Black-Litterman views + mean-variance",
    "inverse_vol": "Inverse volatility",
    "equal_weight": "Equal weight",
}

RETURN_MODELS = {
    "shrunk": "Historical mean shrunk 50% toward the cross-sectional average",
    "historical": "Historical mean (annualised)",
    "ewma": "Exponentially weighted mean",
    "capm": "CAPM implied (beta x market premium)",
    "zero": "None (risk-only objectives)",
}


# ----------------------------------------------------------------------------
# Constraints
# ----------------------------------------------------------------------------

@dataclass
class Constraints:
    long_only: bool = True
    budget: float | None = 1.0            # sum(w) == budget  (None = unconstrained net)
    w_min: float = 0.0                    # scalar lower bound per asset
    w_max: float = 1.0                    # scalar upper bound per asset
    bounds: dict = field(default_factory=dict)  # per-asset overrides {ticker: (lo, hi)}
    gross_max: float | None = None        # sum |w| <= gross_max
    net_min: float | None = None
    net_max: float | None = None
    groups: pd.Series | None = None       # ticker -> group label
    group_max: dict = field(default_factory=dict)   # group -> max net weight
    group_min: dict = field(default_factory=dict)
    turnover_max: float | None = None     # sum |w - w0| <= turnover_max
    w0: pd.Series | None = None           # current weights (for turnover)
    max_vol: float | None = None          # annualised
    min_return: float | None = None       # annualised
    benchmark: pd.Series | None = None
    te_max: float | None = None           # annualised tracking error cap
    fixed: dict = field(default_factory=dict)       # ticker -> fixed weight (e.g. hedges)

    def describe(self) -> list[str]:
        out = []
        out.append("Long only" if self.long_only else "Long/short allowed")
        if self.budget is not None:
            out.append(f"Net exposure = {self.budget:.0%}")
        out.append(f"Position bounds [{self.w_min:.0%}, {self.w_max:.0%}]")
        if self.gross_max is not None:
            out.append(f"Gross exposure <= {self.gross_max:.0%}")
        if self.group_max:
            out.append("Group caps: " + ", ".join(f"{k} <= {v:.0%}" for k, v in self.group_max.items()))
        if self.turnover_max is not None:
            out.append(f"Turnover <= {self.turnover_max:.0%}")
        if self.max_vol is not None:
            out.append(f"Volatility <= {self.max_vol:.1%}")
        if self.te_max is not None:
            out.append(f"Tracking error <= {self.te_max:.1%}")
        if self.fixed:
            out.append("Fixed: " + ", ".join(f"{k}={v:.1%}" for k, v in self.fixed.items()))
        return out


def _apply_constraints(w: cp.Variable, tickers: list[str], cons: Constraints, cov: np.ndarray | None,
                       mu: np.ndarray | None) -> list:
    n = len(tickers)
    c = []
    lo = np.full(n, cons.w_min if not cons.long_only else max(cons.w_min, 0.0))
    hi = np.full(n, cons.w_max)
    for i, t in enumerate(tickers):
        if t in cons.bounds:
            lo[i], hi[i] = cons.bounds[t]
        if t in cons.fixed:
            lo[i] = hi[i] = cons.fixed[t]
    c += [w >= lo, w <= hi]
    if cons.budget is not None:
        c.append(cp.sum(w) == cons.budget)
    if cons.net_min is not None:
        c.append(cp.sum(w) >= cons.net_min)
    if cons.net_max is not None:
        c.append(cp.sum(w) <= cons.net_max)
    if cons.gross_max is not None:
        c.append(cp.norm1(w) <= cons.gross_max)
    if cons.groups is not None and (cons.group_max or cons.group_min):
        g = cons.groups.reindex(tickers).fillna("Unclassified").to_numpy()
        for grp, cap in cons.group_max.items():
            mask = (g == grp).astype(float)
            if mask.sum():
                c.append(mask @ w <= cap)
        for grp, floor in cons.group_min.items():
            mask = (g == grp).astype(float)
            if mask.sum():
                c.append(mask @ w >= floor)
    if cons.turnover_max is not None and cons.w0 is not None:
        w0 = cons.w0.reindex(tickers).fillna(0.0).to_numpy()
        c.append(cp.norm1(w - w0) <= cons.turnover_max)
    if cons.max_vol is not None and cov is not None:
        c.append(cp.quad_form(w, cp.psd_wrap(cov)) <= cons.max_vol ** 2)
    if cons.min_return is not None and mu is not None:
        c.append(mu @ w >= cons.min_return)
    if cons.te_max is not None and cons.benchmark is not None and cov is not None:
        b = cons.benchmark.reindex(tickers).fillna(0.0).to_numpy()
        c.append(cp.quad_form(w - b, cp.psd_wrap(cov)) <= cons.te_max ** 2)
    return c


# ----------------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------------

@dataclass
class OptResult:
    method: str
    weights: pd.Series
    status: str
    expected_return: float
    vol: float
    sharpe: float
    message: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("optimal", "optimal_inaccurate")


def _solve(prob: cp.Problem) -> str:
    for solver in ("CLARABEL", "OSQP", "SCS", "ECOS"):
        try:
            prob.solve(solver=solver)
            if prob.status in ("optimal", "optimal_inaccurate"):
                return prob.status
        except (cp.error.SolverError, ValueError):
            continue
    return prob.status or "failed"


def _finish(method: str, w: np.ndarray | None, status: str, tickers, mu: np.ndarray | None,
            cov: np.ndarray, rf: float, message: str = "", extra: dict | None = None) -> OptResult:
    if w is None or status not in ("optimal", "optimal_inaccurate"):
        empty = pd.Series(np.nan, index=tickers)
        return OptResult(method, empty, status, float("nan"), float("nan"), float("nan"),
                         message or f"Optimiser returned status '{status}'. Try relaxing constraints.", extra or {})
    w = np.asarray(w).ravel()
    w[np.abs(w) < 1e-6] = 0.0
    vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    er = float(mu @ w) if mu is not None else float("nan")
    sharpe = (er - rf) / vol if vol > 0 and not np.isnan(er) else float("nan")
    return OptResult(method, pd.Series(w, index=tickers), status, er, vol, sharpe, message, extra or {})


# ----------------------------------------------------------------------------
# Expected returns
# ----------------------------------------------------------------------------

def expected_returns(returns_daily: pd.DataFrame, method: str = "shrunk", halflife: int = 126,
                     market: pd.Series | None = None, rf: float = 0.0, periods: int = TRADING_DAYS) -> pd.Series:
    method = (method or "shrunk").lower()
    if method == "zero":
        return pd.Series(0.0, index=returns_daily.columns)
    if method == "historical":
        return returns_daily.mean() * periods
    if method == "ewma":
        return returns_daily.ewm(halflife=halflife).mean().iloc[-1] * periods
    if method == "capm":
        if market is None:
            raise ValueError("capm requires a market return series")
        m = market.reindex(returns_daily.index)
        betas = returns_daily.apply(lambda c: c.cov(m) / m.var())
        prem = m.mean() * periods - rf
        return rf + betas * prem
    # shrunk (default): James-Stein style shrink toward grand mean
    hist = returns_daily.mean() * periods
    grand = hist.mean()
    return 0.5 * hist + 0.5 * grand


# ----------------------------------------------------------------------------
# Optimisers
# ----------------------------------------------------------------------------

def _prep(cov: pd.DataFrame, mu: pd.Series | None):
    tickers = list(cov.index)
    c = nearest_psd(cov).to_numpy(dtype=float)
    m = mu.reindex(tickers).fillna(0.0).to_numpy(dtype=float) if mu is not None else None
    return tickers, c, m


def min_variance(cov: pd.DataFrame, cons: Constraints, mu: pd.Series | None = None, rf: float = 0.0) -> OptResult:
    tickers, c, m = _prep(cov, mu)
    w = cp.Variable(len(tickers))
    prob = cp.Problem(cp.Minimize(cp.quad_form(w, cp.psd_wrap(c))), _apply_constraints(w, tickers, cons, c, m))
    status = _solve(prob)
    return _finish("min_variance", w.value, status, tickers, m, c, rf)


def mean_variance(mu: pd.Series, cov: pd.DataFrame, cons: Constraints, risk_aversion: float = 5.0,
                  rf: float = 0.0) -> OptResult:
    tickers, c, m = _prep(cov, mu)
    w = cp.Variable(len(tickers))
    obj = cp.Maximize(m @ w - 0.5 * risk_aversion * cp.quad_form(w, cp.psd_wrap(c)))
    prob = cp.Problem(obj, _apply_constraints(w, tickers, cons, c, m))
    status = _solve(prob)
    return _finish("mean_variance", w.value, status, tickers, m, c, rf, extra={"risk_aversion": risk_aversion})


def target_return(mu: pd.Series, cov: pd.DataFrame, cons: Constraints, target: float, rf: float = 0.0) -> OptResult:
    tickers, c, m = _prep(cov, mu)
    w = cp.Variable(len(tickers))
    constraints = _apply_constraints(w, tickers, cons, c, m) + [m @ w >= target]
    prob = cp.Problem(cp.Minimize(cp.quad_form(w, cp.psd_wrap(c))), constraints)
    status = _solve(prob)
    return _finish("target_return", w.value, status, tickers, m, c, rf, extra={"target_return": target})


def target_vol(mu: pd.Series, cov: pd.DataFrame, cons: Constraints, target: float, rf: float = 0.0) -> OptResult:
    tickers, c, m = _prep(cov, mu)
    w = cp.Variable(len(tickers))
    constraints = _apply_constraints(w, tickers, cons, c, m) + [cp.quad_form(w, cp.psd_wrap(c)) <= target ** 2]
    prob = cp.Problem(cp.Maximize(m @ w), constraints)
    status = _solve(prob)
    return _finish("target_vol", w.value, status, tickers, m, c, rf, extra={"target_vol": target})


def max_sharpe(mu: pd.Series, cov: pd.DataFrame, cons: Constraints, rf: float = 0.0, n_grid: int = 24) -> OptResult:
    """Maximum Sharpe via a scan over target returns along the constrained frontier.

    This is robust to any convex constraint set (the classic Schaible transform
    needs homogeneous constraints, which sector caps and turnover limits break).
    """
    tickers, c, m = _prep(cov, mu)
    lo = min_variance(cov, cons, mu, rf)
    if not lo.ok:
        return OptResult("max_sharpe", lo.weights, lo.status, np.nan, np.nan, np.nan, lo.message)
    # highest achievable return
    w = cp.Variable(len(tickers))
    prob = cp.Problem(cp.Maximize(m @ w), _apply_constraints(w, tickers, cons, c, m))
    st = _solve(prob)
    hi_ret = float(m @ w.value) if st in ("optimal", "optimal_inaccurate") else lo.expected_return
    if hi_ret <= lo.expected_return + 1e-9:
        return OptResult("max_sharpe", lo.weights, lo.status, lo.expected_return, lo.vol, lo.sharpe,
                         "Frontier is a single point; returned the minimum-variance portfolio.")
    best = lo
    # golden-section search on target return maximising Sharpe (frontier Sharpe is unimodal)
    a, b = lo.expected_return, hi_ret
    phi = (np.sqrt(5) - 1) / 2

    def sharpe_at(t):
        r = target_return(mu, cov, cons, t, rf)
        return (r.sharpe if r.ok else -np.inf), r

    x1, x2 = b - phi * (b - a), a + phi * (b - a)
    s1, r1 = sharpe_at(x1)
    s2, r2 = sharpe_at(x2)
    for _ in range(n_grid):
        if s1 > s2:
            b, x2, s2, r2 = x2, x1, s1, r1
            x1 = b - phi * (b - a)
            s1, r1 = sharpe_at(x1)
        else:
            a, x1, s1, r1 = x1, x2, s2, r2
            x2 = a + phi * (b - a)
            s2, r2 = sharpe_at(x2)
        if abs(b - a) < 1e-5:
            break
    cand = r1 if s1 >= s2 else r2
    if cand.ok and (np.isnan(best.sharpe) or cand.sharpe > best.sharpe):
        best = cand
    best.method = "max_sharpe"
    return best


def risk_parity(cov: pd.DataFrame, cons: Constraints | None = None, budgets: pd.Series | None = None,
                mu: pd.Series | None = None, rf: float = 0.0) -> OptResult:
    """Equal (or budgeted) risk contribution, long-only.

    Solved via the convex log-barrier formulation (Spinu 2013):
        min 0.5 w'Sw - sum_i b_i log(w_i)
    then normalised to sum to one. Per-asset upper bounds are enforced with an
    SLSQP refinement when supplied.
    """
    tickers, c, m = _prep(cov, mu)
    n = len(tickers)
    b = (budgets.reindex(tickers).fillna(0.0) if budgets is not None else pd.Series(1.0 / n, index=tickers))
    b = b / b.sum()
    bv = b.to_numpy(dtype=float)
    active = bv > 0
    w = cp.Variable(n)
    constraints = [w >= 1e-8]
    if (~active).any():
        constraints.append(w[np.where(~active)[0]] == 1e-8)
    obj = cp.Minimize(0.5 * cp.quad_form(w, cp.psd_wrap(c)) - bv[active] @ cp.log(w[np.where(active)[0]]))
    prob = cp.Problem(obj, constraints)
    status = _solve(prob)
    if status not in ("optimal", "optimal_inaccurate"):
        return _finish("risk_parity", None, status, tickers, m, c, rf)
    wv = np.asarray(w.value).ravel()
    wv = wv / wv.sum()
    if cons is not None and (cons.w_max < 1.0 or cons.bounds):
        wv = _risk_parity_slsqp(c, bv, cons, tickers, wv)
    return _finish("risk_parity", wv, "optimal", tickers, m, c, rf, extra={"budgets": b.to_dict()})


def _risk_parity_slsqp(c, bv, cons: Constraints, tickers, w_init):
    n = len(tickers)
    lo = np.full(n, max(cons.w_min, 0.0))
    hi = np.full(n, cons.w_max)
    for i, t in enumerate(tickers):
        if t in cons.bounds:
            lo[i], hi[i] = cons.bounds[t]

    def obj(w):
        var = w @ c @ w
        rc = w * (c @ w) / var if var > 0 else np.zeros_like(w)
        return float(((rc - bv) ** 2).sum()) * 1e4

    res = sopt.minimize(obj, w_init, method="SLSQP", bounds=list(zip(lo, hi)),
                        constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0}],
                        options={"maxiter": 500, "ftol": 1e-12})
    return res.x if res.success else w_init


def max_diversification(cov: pd.DataFrame, cons: Constraints | None = None, mu: pd.Series | None = None,
                        rf: float = 0.0) -> OptResult:
    """Maximise (w' sigma) / sqrt(w' S w), long-only, via the standard convex transform."""
    tickers, c, m = _prep(cov, mu)
    sd = np.sqrt(np.diag(c))
    y = cp.Variable(len(tickers))
    prob = cp.Problem(cp.Minimize(cp.quad_form(y, cp.psd_wrap(c))), [sd @ y == 1.0, y >= 0])
    status = _solve(prob)
    if status not in ("optimal", "optimal_inaccurate"):
        return _finish("max_diversification", None, status, tickers, m, c, rf)
    wv = np.asarray(y.value).ravel()
    wv = np.clip(wv, 0, None)
    wv = wv / wv.sum()
    if cons is not None and cons.w_max < 1.0:
        wv = _cap_and_redistribute(wv, cons.w_max)
    return _finish("max_diversification", wv, "optimal", tickers, m, c, rf)


def inverse_vol(cov: pd.DataFrame, mu: pd.Series | None = None, rf: float = 0.0) -> OptResult:
    tickers, c, m = _prep(cov, mu)
    inv = 1.0 / np.sqrt(np.diag(c))
    wv = inv / inv.sum()
    return _finish("inverse_vol", wv, "optimal", tickers, m, c, rf)


def equal_weight(cov: pd.DataFrame, mu: pd.Series | None = None, rf: float = 0.0) -> OptResult:
    tickers, c, m = _prep(cov, mu)
    wv = np.full(len(tickers), 1.0 / len(tickers))
    return _finish("equal_weight", wv, "optimal", tickers, m, c, rf)


def _cap_and_redistribute(w: np.ndarray, cap: float, iters: int = 50) -> np.ndarray:
    w = w.copy()
    for _ in range(iters):
        over = w > cap
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        free = ~over
        if free.sum() == 0 or w[free].sum() == 0:
            break
        w[free] += excess * w[free] / w[free].sum()
    return w


# ----------------------------------------------------------------------------
# Black-Litterman
# ----------------------------------------------------------------------------

def black_litterman(cov: pd.DataFrame, market_weights: pd.Series, views: list[dict],
                    tau: float = 0.05, risk_aversion: float | None = None, rf: float = 0.0,
                    market_premium: float = 0.05) -> dict:
    """Black-Litterman posterior expected returns.

    views: list of {"assets": {ticker: coefficient}, "return": q, "confidence": 0-1}
      absolute view  -> {"AAPL": 1.0}, return 0.10
      relative view  -> {"AAPL": 1.0, "MSFT": -1.0}, return 0.03  (AAPL beats MSFT by 3%)
    Confidence maps to the view variance via omega = (1/conf - 1) * tau * P S P'.
    """
    tickers = list(cov.index)
    S = cov.to_numpy(dtype=float)
    w_mkt = market_weights.reindex(tickers).fillna(0.0).to_numpy(dtype=float)
    if w_mkt.sum() > 0:
        w_mkt = w_mkt / w_mkt.sum()
    if risk_aversion is None:
        mkt_var = float(w_mkt @ S @ w_mkt)
        risk_aversion = market_premium / mkt_var if mkt_var > 0 else 2.5
    pi = risk_aversion * S @ w_mkt  # implied equilibrium excess returns
    if not views:
        return {"prior": pd.Series(pi + rf, index=tickers), "posterior": pd.Series(pi + rf, index=tickers),
                "posterior_cov": cov, "risk_aversion": risk_aversion, "views": []}
    P = np.zeros((len(views), len(tickers)))
    Q = np.zeros(len(views))
    omega_diag = np.zeros(len(views))
    for k, v in enumerate(views):
        for t, coef in v["assets"].items():
            if t in tickers:
                P[k, tickers.index(t)] = coef
        Q[k] = v["return"]
        conf = float(np.clip(v.get("confidence", 0.5), 0.01, 0.99))
        pv = float(P[k] @ (tau * S) @ P[k])
        omega_diag[k] = (1.0 / conf - 1.0) * pv if pv > 0 else 1e-6
    Omega = np.diag(np.maximum(omega_diag, 1e-10))
    tauS = tau * S
    middle = np.linalg.inv(P @ tauS @ P.T + Omega)
    mu_bl = pi + tauS @ P.T @ middle @ (Q - P @ pi)
    cov_bl = S + tauS - tauS @ P.T @ middle @ P @ tauS
    return {
        "prior": pd.Series(pi + rf, index=tickers),
        "posterior": pd.Series(mu_bl + rf, index=tickers),
        "posterior_cov": pd.DataFrame(cov_bl, index=tickers, columns=tickers),
        "risk_aversion": float(risk_aversion),
        "views": views,
    }


# ----------------------------------------------------------------------------
# Frontier and sizing
# ----------------------------------------------------------------------------

def efficient_frontier(mu: pd.Series, cov: pd.DataFrame, cons: Constraints, n_points: int = 20,
                       rf: float = 0.0) -> list[dict]:
    lo = min_variance(cov, cons, mu, rf)
    if not lo.ok:
        return []
    tickers, c, m = _prep(cov, mu)
    w = cp.Variable(len(tickers))
    prob = cp.Problem(cp.Maximize(m @ w), _apply_constraints(w, tickers, cons, c, m))
    st = _solve(prob)
    hi_ret = float(m @ w.value) if st in ("optimal", "optimal_inaccurate") else lo.expected_return
    pts = []
    for t in np.linspace(lo.expected_return, hi_ret, n_points):
        r = target_return(mu, cov, cons, float(t), rf)
        if r.ok:
            pts.append({"target": float(t), "return": r.expected_return, "vol": r.vol,
                        "sharpe": r.sharpe, "weights": r.weights.round(4).to_dict()})
    return pts


def vol_target_scale(weights: pd.Series, cov: pd.DataFrame, target_vol: float) -> dict:
    """Scale the whole book (leverage) to hit a target annualised volatility."""
    w = weights.reindex(cov.index).fillna(0.0).to_numpy(dtype=float)
    c = cov.to_numpy(dtype=float)
    cur = float(np.sqrt(max(w @ c @ w, 0.0)))
    k = target_vol / cur if cur > 0 else float("nan")
    return {"current_vol": cur, "target_vol": target_vol, "scale": k,
            "scaled_weights": pd.Series(w * k, index=cov.index),
            "gross_before": float(np.abs(w).sum()), "gross_after": float(np.abs(w * k).sum())}


def kelly_weights(mu: pd.Series, cov: pd.DataFrame, rf: float = 0.0, fraction: float = 0.5) -> dict:
    """Unconstrained (fractional) Kelly: f = fraction x S^-1 (mu - rf)."""
    tickers, c, m = _prep(cov, mu)
    excess = m - rf
    f = fraction * np.linalg.solve(c + np.eye(len(tickers)) * 1e-8, excess)
    return {"weights": pd.Series(f, index=tickers), "gross": float(np.abs(f).sum()), "net": float(f.sum()),
            "vol": float(np.sqrt(max(f @ c @ f, 0.0))), "expected_return": float(m @ f)}


def trim_to_risk_share(weights: pd.Series, cov: pd.DataFrame, ticker: str, target_pct: float) -> float | None:
    """Return the weight of `ticker` at which its share of portfolio vol equals target_pct
    (holding all other weights fixed). None if it cannot be reached by trimming."""
    if ticker not in cov.index:
        return None
    w = weights.reindex(cov.index).fillna(0.0).astype(float)
    c = cov.to_numpy(dtype=float)
    i = list(cov.index).index(ticker)
    w0 = float(w.iloc[i])
    if w0 == 0:
        return None

    def share(x):
        ww = w.to_numpy().copy()
        ww[i] = x
        var = ww @ c @ ww
        return (ww[i] * (c @ ww)[i] / var) if var > 0 else 0.0

    lo, hi = (0.0, w0) if w0 > 0 else (w0, 0.0)
    if share(w0) <= target_pct:
        return w0
    for _ in range(60):
        mid = (lo + hi) / 2
        if (share(mid) > target_pct) == (w0 > 0):
            hi = mid if w0 > 0 else hi
            lo = lo if w0 > 0 else mid
        else:
            lo = mid if w0 > 0 else lo
            hi = hi if w0 > 0 else mid
    return (lo + hi) / 2


def run(method: str, mu: pd.Series | None, cov: pd.DataFrame, cons: Constraints, rf: float = 0.0, **kw) -> OptResult:
    """Dispatch by method name."""
    method = method.lower()
    if method == "min_variance":
        return min_variance(cov, cons, mu, rf)
    if method == "max_sharpe":
        return max_sharpe(mu, cov, cons, rf)
    if method == "mean_variance":
        return mean_variance(mu, cov, cons, kw.get("risk_aversion", 5.0), rf)
    if method == "target_return":
        return target_return(mu, cov, cons, kw.get("target", 0.08), rf)
    if method == "target_vol":
        return target_vol(mu, cov, cons, kw.get("target", 0.12), rf)
    if method == "risk_parity":
        return risk_parity(cov, cons, kw.get("budgets"), mu, rf)
    if method == "max_diversification":
        return max_diversification(cov, cons, mu, rf)
    if method == "inverse_vol":
        return inverse_vol(cov, mu, rf)
    if method == "equal_weight":
        return equal_weight(cov, mu, rf)
    if method == "black_litterman":
        bl = black_litterman(cov, kw.get("market_weights", pd.Series(1.0, index=cov.index)),
                             kw.get("views", []), kw.get("tau", 0.05), rf=rf)
        res = mean_variance(bl["posterior"], bl["posterior_cov"], cons, kw.get("risk_aversion", 5.0), rf)
        res.method = "black_litterman"
        res.extra.update({"prior": bl["prior"], "posterior": bl["posterior"], "risk_aversion_implied": bl["risk_aversion"]})
        return res
    raise ValueError(f"Unknown method {method}")
