"""PortfolioAnalyzer: one object that turns positions + prices into every risk view.

It is deliberately framework-agnostic. The Django layer builds it from the
database (see web/services.py); tests and notebooks can build it from CSVs.
"""
from __future__ import annotations

from functools import cached_property

import numpy as np
import pandas as pd

from . import concentration as conc
from . import factors as fm
from . import liquidity as lq
from . import risk as rk
from . import stress as st
from .covariance import cov_to_corr, estimate_cov
from .insights import generate_insights
from .returns import TRADING_DAYS, simple_returns, window

DEFAULT_SETTINGS = {
    "lookback_days": 504,
    "cov_method": "ledoit_wolf",
    "ewma_halflife": 60,
    "var_confidence": 0.99,
    "var_horizon_days": 1,
    "risk_free_rate": 0.04,
    "benchmark": "SPY",
    "liquidity_participation": 0.20,
    "target_beta": None,
}


class PortfolioAnalyzer:
    def __init__(self, prices: pd.DataFrame, volumes: pd.DataFrame | None, positions: pd.DataFrame,
                 cash: float = 0.0, settings: dict | None = None, name: str = "Portfolio"):
        """
        prices:    wide DataFrame of adjusted closes (all tickers incl. factor ETFs)
        volumes:   wide DataFrame of share volumes (same shape) or None
        positions: DataFrame indexed by ticker with columns quantity, and optionally
                   sector, asset_class, name, industry, country
        cash:      cash balance in base currency (can be negative = borrowed)
        """
        self.name = name
        self.warnings: list[str] = []
        self.prices = prices.sort_index()
        self.volumes = volumes.sort_index() if volumes is not None else None
        pos = positions.copy()
        pos = pos[pos["quantity"] != 0]
        missing = [t for t in pos.index if t not in self.prices.columns]
        if missing:
            self.warnings.append(f"No price history for {', '.join(missing)}: excluded from NAV and all risk figures. "
                                 "Load prices with `manage.py fetch_prices --tickers ...`.")
        pos = pos[pos.index.isin(self.prices.columns)]
        for col in ("sector", "asset_class", "name", "industry", "country"):
            if col not in pos.columns:
                pos[col] = "Unclassified" if col != "name" else pos.index
            pos[col] = pos[col].fillna("Unclassified")
        self.positions = pos
        self.cash = float(cash)
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}

    # ------------------------------------------------------------------ basics
    @property
    def tickers(self) -> list[str]:
        return list(self.positions.index)

    @cached_property
    def as_of(self) -> pd.Timestamp:
        return self.prices.index[-1]

    @cached_property
    def last_prices(self) -> pd.Series:
        return self.prices.ffill().iloc[-1]

    @cached_property
    def market_values(self) -> pd.Series:
        return (self.positions["quantity"] * self.last_prices.reindex(self.tickers)).astype(float)

    @cached_property
    def nav(self) -> float:
        return float(self.market_values.sum() + self.cash)

    @cached_property
    def weights(self) -> pd.Series:
        nav = self.nav
        if nav == 0:
            return self.market_values * 0.0
        return self.market_values / nav

    def summary(self) -> dict:
        mv = self.market_values
        longs = mv[mv > 0].sum()
        shorts = mv[mv < 0].sum()
        nav = self.nav
        perf = self.performance
        var_c = self.settings["var_confidence"]
        return {
            "name": self.name,
            "as_of": self.as_of,
            "nav": nav,
            "cash": self.cash,
            "cash_weight": self.cash / nav if nav else float("nan"),
            "long_value": float(longs),
            "short_value": float(shorts),
            "gross_value": float(longs - shorts),
            "net_value": float(longs + shorts),
            "long_exposure": float(longs / nav) if nav else float("nan"),
            "short_exposure": float(shorts / nav) if nav else float("nan"),
            "gross_exposure": float((longs - shorts) / nav) if nav else float("nan"),
            "net_exposure": float((longs + shorts) / nav) if nav else float("nan"),
            "n_positions": int(len(self.positions)),
            "n_long": int((mv > 0).sum()),
            "n_short": int((mv < 0).sum()),
            "vol": self.portfolio_vol,
            "beta": perf.get("beta", float("nan")),
            "var": self.var_main.var,
            "cvar": self.var_main.cvar,
            "var_confidence": var_c,
            "var_horizon_days": self.settings["var_horizon_days"],
            "var_dollar": self.var_main.var * nav,
            "cvar_dollar": self.var_main.cvar * nav,
            "sharpe": perf.get("sharpe", float("nan")),
            "ytd_return": perf.get("ytd_return", float("nan")),
            "ret_1m": perf.get("ret_1m", float("nan")),
            "max_drawdown": perf.get("max_drawdown", float("nan")),
            "current_drawdown": perf.get("current_drawdown", float("nan")),
            "lookback_days": int(len(self.window_returns)),
            "cov_method": self.settings["cov_method"],
            "benchmark": self.settings["benchmark"],
            "shrinkage": self.cov.attrs.get("shrinkage"),
        }

    # ------------------------------------------------------------------ returns/cov
    @cached_property
    def asset_returns(self) -> pd.DataFrame:
        return simple_returns(self.prices[self.tickers])

    @cached_property
    def window_returns(self) -> pd.DataFrame:
        r = window(self.asset_returns, self.settings["lookback_days"])
        kept = r.dropna(axis=1, thresh=int(len(r) * 0.6))
        dropped = [t for t in r.columns if t not in kept.columns]
        if dropped:
            self.warnings.append(f"{', '.join(dropped)} cover less than 60% of the {len(r)}-day lookback window: "
                                 "held in NAV and exposures but excluded from the covariance, VaR and factor model. "
                                 "Shorten the lookback in Analytics settings to include them.")
        return kept

    def all_warnings(self) -> list[str]:
        """Warnings collected while building the risk model (forces the lazy pieces that can warn)."""
        _ = self.window_returns
        return list(dict.fromkeys(self.warnings))

    @cached_property
    def cov(self) -> pd.DataFrame:
        return estimate_cov(self.window_returns, self.settings["cov_method"], self.settings["ewma_halflife"])

    @cached_property
    def corr(self) -> pd.DataFrame:
        return cov_to_corr(self.cov)

    @cached_property
    def cov_annual(self) -> pd.DataFrame:
        return self.cov * TRADING_DAYS

    @cached_property
    def benchmark_returns(self) -> pd.Series | None:
        b = self.settings["benchmark"]
        if b and b in self.prices.columns:
            return simple_returns(self.prices[b])
        return None

    @cached_property
    def port_returns(self) -> pd.Series:
        """Pro-forma daily returns of the *current* book held over history."""
        w = self.weights.reindex(self.asset_returns.columns).fillna(0.0)
        return (self.asset_returns.fillna(0.0) @ w).rename("portfolio")

    @cached_property
    def port_returns_window(self) -> pd.Series:
        return window(self.port_returns.to_frame(), self.settings["lookback_days"])["portfolio"]

    @cached_property
    def performance(self) -> dict:
        b = self.benchmark_returns
        return rk.performance_stats(self.port_returns_window, rf=self.settings["risk_free_rate"], benchmark=b)

    # ------------------------------------------------------------------ risk
    @cached_property
    def rc(self) -> pd.DataFrame:
        out = rk.risk_contributions(self.weights, self.cov)
        out = out.join(self.positions[["sector", "asset_class", "name"]], how="left")
        out["market_value"] = self.market_values.reindex(out.index)
        out = out.sort_values("pct", ascending=False)
        out.attrs["portfolio_vol"] = self.portfolio_vol
        return out

    @cached_property
    def portfolio_vol(self) -> float:
        w = self.weights.reindex(self.cov.index).fillna(0.0).to_numpy(dtype=float)
        return rk.portfolio_vol(w, self.cov.to_numpy(dtype=float))

    @cached_property
    def sector_rc(self) -> pd.DataFrame:
        return rk.group_risk_contributions(self.rc, self.positions["sector"])

    @cached_property
    def asset_class_rc(self) -> pd.DataFrame:
        return rk.group_risk_contributions(self.rc, self.positions["asset_class"])

    @cached_property
    def var_main(self) -> rk.VaRResult:
        return rk.historical_var(self.port_returns_window, self.settings["var_confidence"],
                                 self.settings["var_horizon_days"])

    @cached_property
    def var_table(self) -> list[rk.VaRResult]:
        w = self.weights.reindex(self.cov.index).fillna(0.0).to_numpy()
        return rk.var_suite(self.port_returns_window, w, self.cov.to_numpy(),
                            confidences=(0.95, 0.99), horizon_days=self.settings["var_horizon_days"])

    @cached_property
    def var_contrib(self) -> pd.DataFrame:
        return rk.var_contributions(self.weights, self.window_returns, self.settings["var_confidence"])

    @cached_property
    def drawdowns(self) -> pd.DataFrame:
        return rk.drawdown_table(self.port_returns)

    def rolling(self, window_vol: int = 21, window_beta: int = 63) -> dict:
        out = {"vol": rk.rolling_vol(self.port_returns, window_vol)}
        if self.benchmark_returns is not None:
            out["beta"] = rk.rolling_beta(self.port_returns, self.benchmark_returns, window_beta)
            out["bench_vol"] = rk.rolling_vol(self.benchmark_returns, window_vol)
        out["drawdown"] = rk.drawdown_series(self.port_returns)
        return out

    # ------------------------------------------------------------------ factors
    @cached_property
    def factor_returns(self) -> pd.DataFrame:
        etfs = [t for t in fm.required_tickers() if t in self.prices.columns]
        return fm.build_factor_returns(simple_returns(self.prices[etfs]))

    @cached_property
    def factor_model(self) -> dict:
        fr = window(self.factor_returns, self.settings["lookback_days"])
        return fm.factor_regression(self.window_returns, fr)

    @cached_property
    def factor_decomposition(self) -> dict:
        return fm.factor_risk_decomposition(self.weights, self.factor_model)

    @cached_property
    def factor_exposures(self) -> pd.Series:
        return fm.portfolio_factor_exposures(self.weights, self.factor_model["betas"])

    # ------------------------------------------------------------------ concentration / liquidity
    @cached_property
    def concentration(self) -> dict:
        w = self.weights
        return {
            "hhi": conc.hhi(w),
            "effective_n": conc.effective_n(w),
            "top1": conc.top_n_weight(w, 1),
            "top5": conc.top_n_weight(w, 5),
            "top10": conc.top_n_weight(w, 10),
            "div_ratio": conc.diversification_ratio(w, self.cov),
            "enb": conc.effective_number_of_bets(w, self.cov),
            "n_positions": int(len(w)),
            "max_weight": float(w.abs().max()) if len(w) else float("nan"),
            "max_weight_ticker": str(w.abs().idxmax()) if len(w) else "",
        }

    @cached_property
    def clusters(self) -> list[dict]:
        return conc.pairwise_clusters(self.corr, self.weights, threshold=0.75)

    @cached_property
    def sector_weights(self) -> pd.DataFrame:
        return conc.group_weights(self.weights, self.positions["sector"])

    @cached_property
    def asset_class_weights(self) -> pd.DataFrame:
        return conc.group_weights(self.weights, self.positions["asset_class"])

    @cached_property
    def liquidity(self) -> pd.DataFrame:
        if self.volumes is None:
            return pd.DataFrame()
        adv = lq.average_daily_volume(self.volumes[self.tickers])
        table = lq.liquidity_table(self.positions["quantity"], adv, self.last_prices, self.nav,
                                   self.settings["liquidity_participation"])
        daily_vol = self.window_returns.std()
        table["impact_cost"] = lq.liquidation_cost_estimate(table, daily_vol)
        return table.join(self.positions[["sector", "name"]], how="left")

    @cached_property
    def liquidity_profile(self) -> pd.DataFrame:
        if self.liquidity.empty:
            return pd.DataFrame()
        return lq.liquidity_profile(self.liquidity)

    # ------------------------------------------------------------------ stress
    @cached_property
    def stress_historical(self) -> list[dict]:
        betas = self.factor_model["betas"]
        etfs = [t for t in fm.required_tickers() if t in self.prices.columns]
        out = []
        for sc in st.HISTORICAL_SCENARIOS:
            res = st.historical_replay(self.weights, self.prices[self.tickers], sc["start"], sc["end"],
                                       betas=betas, factor_prices=self.prices[etfs], factor_spec=fm.FACTOR_SPEC)
            res.update({k: sc[k] for k in ("key", "name", "description")})
            res["kind"] = "historical"
            if res.get("available"):
                res["total_dollar"] = res["total_pnl"] * self.nav
                bench = self.settings["benchmark"]
                if bench in self.prices.columns:
                    seg = self.prices[bench].loc[sc["start"]:sc["end"]]
                    res["benchmark_return"] = float(seg.iloc[-1] / seg.iloc[0] - 1) if len(seg) > 1 else float("nan")
            out.append(res)
        return out

    @cached_property
    def stress_hypothetical(self) -> list[dict]:
        betas = self.factor_model["betas"]
        out = []
        for sc in st.HYPOTHETICAL_SCENARIOS:
            res = st.hypothetical_shock(self.weights, betas, sc["shocks"])
            res.update({k: sc[k] for k in ("key", "name", "description")})
            res["kind"] = "hypothetical"
            res["total_dollar"] = res["total_pnl"] * self.nav
            out.append(res)
        return out

    def custom_shock(self, shocks: dict[str, float]) -> dict:
        res = st.hypothetical_shock(self.weights, self.factor_model["betas"], shocks)
        res["total_dollar"] = res["total_pnl"] * self.nav
        res["name"] = "Custom shock"
        return res

    @cached_property
    def single_name_shocks(self) -> pd.DataFrame:
        return st.single_asset_shocks(self.weights, self.corr, self.window_returns.std())

    @cached_property
    def worst_windows(self) -> pd.DataFrame:
        return st.worst_windows(self.port_returns, 10, 5)

    # ------------------------------------------------------------------ limits
    def metric_value(self, metric: str, scope: str | None = None) -> tuple[float | None, str]:
        """Return (value, display) for a limit metric. Scope narrows to a ticker/sector/factor."""
        s = self.summary()
        pct = lambda v: f"{v * 100:.1f}%"  # noqa: E731
        num = lambda v: f"{v:.2f}"  # noqa: E731
        w = self.weights
        if metric == "gross_exposure":
            return s["gross_exposure"], pct(s["gross_exposure"])
        if metric == "net_exposure":
            return s["net_exposure"], pct(s["net_exposure"])
        if metric == "long_exposure":
            return s["long_exposure"], pct(s["long_exposure"])
        if metric == "short_exposure":
            return abs(s["short_exposure"]), pct(abs(s["short_exposure"]))
        if metric == "position_weight":
            # scope: a ticker, "equity" (single stocks only), or blank (all positions)
            if scope and scope in w.index:
                v = abs(float(w[scope]))
            elif scope and scope.lower() in ("equity", "stocks", "single_names"):
                mask = self.positions["asset_class"].reindex(w.index) == "Equity"
                sub = w[mask.fillna(False)]
                v = float(sub.abs().max()) if len(sub) else 0.0
            else:
                v = float(w.abs().max()) if len(w) else 0.0
            return v, pct(v)
        if metric == "sector_gross":
            sw = self.sector_weights
            v = float(sw.loc[scope, "gross"]) if scope in sw.index else (float(sw["gross"].max()) if len(sw) else 0.0)
            return v, pct(v)
        if metric == "sector_net":
            sw = self.sector_weights
            v = float(sw.loc[scope, "net"]) if scope in sw.index else (float(sw["net"].abs().max()) if len(sw) else 0.0)
            return v, pct(v)
        if metric == "beta":
            return s["beta"], num(s["beta"])
        if metric == "vol":
            return s["vol"], pct(s["vol"])
        if metric == "var":
            return s["var"], pct(s["var"])
        if metric == "cvar":
            return s["cvar"], pct(s["cvar"])
        if metric == "top5_weight":
            return self.concentration["top5"], pct(self.concentration["top5"])
        if metric == "max_risk_share":
            v = float(self.rc["pct"].max()) if len(self.rc) else 0.0
            return v, pct(v)
        if metric == "effective_n":
            return self.concentration["effective_n"], num(self.concentration["effective_n"])
        if metric == "days_to_liquidate":
            if self.liquidity.empty:
                return None, "n/a"
            if scope and scope in self.liquidity.index:
                v = float(self.liquidity.loc[scope, "days_to_liquidate"])
            else:
                v = float(self.liquidity["days_to_liquidate"].replace(np.inf, 999).max())
            return v, f"{v:.2f}d"
        if metric == "factor_beta":
            ex = self.factor_exposures
            if scope in ex.index:
                v = abs(float(ex[scope]))
                return v, num(v)
            return None, "n/a"
        if metric == "stress_loss":
            worst = min(self.stress_historical + self.stress_hypothetical,
                        key=lambda d: d.get("total_pnl", 0))
            v = abs(float(worst.get("total_pnl", 0)))
            return v, pct(v)
        if metric == "drawdown":
            v = abs(s["current_drawdown"])
            return v, pct(v)
        return None, "n/a"

    def check_limits(self, limits: list[dict]) -> list[dict]:
        """limits: [{id, metric, scope, operator ('lte'|'gte'), threshold, label}]"""
        out = []
        for lim in limits:
            value, display = self.metric_value(lim["metric"], lim.get("scope"))
            thr = float(lim["threshold"])
            op = lim.get("operator", "lte")
            is_pct = lim["metric"] not in ("beta", "effective_n", "days_to_liquidate", "factor_beta")
            disp_thr = (f"{thr * 100:.1f}%" if is_pct else (f"{thr:.1f}d" if lim["metric"] == "days_to_liquidate" else f"{thr:.2f}"))
            status, util = "n/a", None
            if value is not None and not (isinstance(value, float) and np.isnan(value)):
                if op == "lte":
                    util = value / thr if thr else float("inf")
                    status = "breach" if value > thr else ("warn" if util >= 0.9 else "ok")
                else:
                    util = thr / value if value else float("inf")
                    status = "breach" if value < thr else ("warn" if util >= 0.9 else "ok")
            out.append({**lim, "value": value, "display_value": display, "display_threshold": disp_thr,
                        "status": status, "utilization": util})
        return out

    # ------------------------------------------------------------------ insights
    def insights(self, limit_results: list[dict] | None = None) -> list:
        hedge = {"beta": self.performance.get("beta"), "benchmark": self.settings["benchmark"],
                 "target_beta": self.settings.get("target_beta")}
        ctx = {
            "summary": self.summary(),
            "rc": self.rc,
            "sector_rc": self.sector_rc,
            "cov": self.cov,
            "weights": self.weights,
            "factor": self.factor_decomposition,
            "limits": limit_results or [],
            "concentration": self.concentration,
            "liquidity": self.liquidity,
            "stress": [d for d in self.stress_historical + self.stress_hypothetical if d.get("available")],
            "clusters": self.clusters,
            "perf": self.performance,
            "hedge": hedge,
        }
        return generate_insights(ctx)

    # ------------------------------------------------------------------ what-if
    def with_trades(self, trades: dict[str, float], asset_meta: pd.DataFrame | None = None,
                    cash_delta: float | None = None, name: str | None = None) -> "PortfolioAnalyzer":
        """Return a new analyzer with share deltas applied. Cash is adjusted by the
        traded value at last price unless cash_delta is given."""
        pos = self.positions.copy()
        traded_value = 0.0
        for t, dq in trades.items():
            if dq == 0:
                continue
            if t not in self.prices.columns:
                raise ValueError(f"No price history for {t}")
            px = float(self.last_prices[t])
            traded_value += dq * px
            if t in pos.index:
                pos.loc[t, "quantity"] = float(pos.loc[t, "quantity"]) + dq
            else:
                row = {"quantity": dq, "sector": "Unclassified", "asset_class": "Equity", "name": t,
                       "industry": "", "country": ""}
                if asset_meta is not None and t in asset_meta.index:
                    for c in ("sector", "asset_class", "name", "industry", "country"):
                        if c in asset_meta.columns:
                            row[c] = asset_meta.loc[t, c]
                pos.loc[t] = pd.Series(row)
        pos = pos[pos["quantity"] != 0]
        cash = self.cash - traded_value if cash_delta is None else self.cash + cash_delta
        return PortfolioAnalyzer(self.prices, self.volumes, pos, cash, self.settings, name or f"{self.name} (pro forma)")

    def weights_to_shares(self, target_weights: pd.Series) -> pd.DataFrame:
        """Convert target weights (of NAV) into a share trade list vs current holdings."""
        rows = []
        for t, tw in target_weights.items():
            if t not in self.prices.columns:
                continue
            px = float(self.last_prices[t])
            cur_q = float(self.positions["quantity"].get(t, 0.0))
            cur_w = float(self.weights.get(t, 0.0))
            tgt_q = np.floor(tw * self.nav / px) if px > 0 else 0.0
            rows.append({"ticker": t, "current_weight": cur_w, "target_weight": float(tw),
                         "delta_weight": float(tw) - cur_w, "current_shares": cur_q,
                         "target_shares": tgt_q, "delta_shares": tgt_q - cur_q, "price": px,
                         "trade_value": (tgt_q - cur_q) * px})
        df = pd.DataFrame(rows).set_index("ticker") if rows else pd.DataFrame()
        return df.sort_values("delta_weight", key=np.abs, ascending=False) if len(df) else df
