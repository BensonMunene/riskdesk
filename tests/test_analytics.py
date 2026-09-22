"""Unit tests for the analytics library (no database; uses the bundled CSVs)."""
import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from analytics import backtest as bt
from analytics import optimize as opt
from analytics.covariance import estimate_cov, ledoit_wolf_cov
from analytics.engine import PortfolioAnalyzer
from analytics.factors import build_factor_returns, factor_regression, required_tickers
from analytics.returns import TRADING_DAYS, simple_returns
from analytics.risk import cornish_fisher_var, historical_var, parametric_var, risk_contributions

DATA = Path(__file__).resolve().parents[1] / "data" / "sample"


def load_sample(index=0):
    close = pd.read_csv(DATA / "prices_close.csv", index_col=0, parse_dates=True)
    volume = pd.read_csv(DATA / "prices_volume.csv", index_col=0, parse_dates=True)
    assets = pd.read_csv(DATA / "assets.csv").set_index("ticker")
    p = json.loads((DATA / "portfolios.json").read_text())[index]
    last = close.iloc[-1]
    pos = pd.DataFrame({"quantity": {t: np.floor(abs(w) * p["nav"] / last[t]) * np.sign(w) for t, w in p["weights"].items()}})
    pos = pos.join(assets[["name", "sector", "asset_class", "industry", "country"]])
    cash = p["nav"] - (pos["quantity"] * last.reindex(pos.index)).sum()
    return close, volume, assets, pos, cash, p


class AnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.close, cls.volume, cls.assets, cls.pos, cls.cash, cls.spec = load_sample(0)
        cls.a = PortfolioAnalyzer(cls.close, cls.volume, cls.pos, cls.cash, cls.spec["settings"], "test")

    def test_nav_and_weights(self):
        self.assertAlmostEqual(self.a.nav, self.spec["nav"], places=2)
        self.assertAlmostEqual(float(self.a.weights.sum()) + self.a.cash / self.a.nav, 1.0, places=9)

    def test_risk_contributions_sum_to_portfolio_vol(self):
        rc = self.a.rc
        self.assertAlmostEqual(float(rc["pct"].sum()), 1.0, places=9)
        self.assertAlmostEqual(float(rc["ccr"].sum()), self.a.portfolio_vol, places=9)
        self.assertTrue(0.03 < self.a.portfolio_vol < 0.30)

    def test_short_index_hedge_reduces_risk(self):
        self.assertLess(float(self.a.rc.loc["SPY", "pct"]), 0.0)

    def test_var_ordering(self):
        pr = self.a.port_returns_window
        h95, h99 = historical_var(pr, 0.95), historical_var(pr, 0.99)
        self.assertGreater(h99.var, h95.var)
        self.assertGreaterEqual(h99.cvar, h99.var)
        p99 = parametric_var(pr, 0.99)
        self.assertGreater(p99.var, 0)
        cf = cornish_fisher_var(pr, 0.99)
        self.assertGreater(cf.cvar, cf.var)

    def test_ledoit_wolf_is_psd_with_sane_shrinkage(self):
        cov = ledoit_wolf_cov(self.a.window_returns)
        eig = np.linalg.eigvalsh(cov.to_numpy())
        self.assertGreater(eig.min(), -1e-12)
        self.assertTrue(0.0 <= cov.attrs["shrinkage"] <= 1.0)

    def test_factor_model_spy_has_unit_market_beta(self):
        etfs = [t for t in required_tickers() if t in self.close.columns]
        fr = build_factor_returns(simple_returns(self.close[etfs])).iloc[-504:]
        reg = factor_regression(simple_returns(self.close[["SPY", "AAPL"]]).iloc[-504:], fr)
        self.assertAlmostEqual(float(reg["betas"].loc["SPY", "MKT"]), 1.0, places=6)
        self.assertGreater(float(reg["r2"]["SPY"]), 0.999)
        self.assertGreater(float(reg["betas"].loc["AAPL", "MKT"]), 0.5)

    def test_factor_decomposition_matches_total(self):
        fd = self.a.factor_decomposition
        self.assertAlmostEqual(fd["systematic_vol"] ** 2 + fd["idiosyncratic_vol"] ** 2, fd["total_vol"] ** 2, places=10)
        self.assertTrue(0 < fd["systematic_share"] < 1)

    def test_with_trades_keeps_nav_and_adjusts_cash(self):
        b = self.a.with_trades({"NVDA": -5000, "AMD": 8000}, self.assets)
        self.assertAlmostEqual(b.nav, self.a.nav, places=4)
        self.assertIn("AMD", b.positions.index)
        self.assertEqual(float(b.positions.loc["NVDA", "quantity"]), float(self.a.positions.loc["NVDA", "quantity"]) - 5000)
        self.assertNotEqual(b.cash, self.a.cash)

    def test_limits_and_insights(self):
        limits = [{**lim, "id": i} for i, lim in enumerate(self.spec["limits"])]
        res = self.a.check_limits(limits)
        self.assertEqual(len(res), len(limits))
        statuses = {r["status"] for r in res}
        self.assertTrue(statuses <= {"ok", "warn", "breach", "n/a"})
        ins = self.a.insights(res)
        self.assertGreater(len(ins), 0)
        self.assertTrue(all(i.severity in ("high", "medium", "low", "info") for i in ins))

    def test_stress_scenarios_available(self):
        hist = [d for d in self.a.stress_historical if d.get("available")]
        self.assertGreaterEqual(len(hist), 6)
        covid = next(d for d in hist if d["key"] == "covid_crash")
        self.assertLess(covid["total_pnl"], 0)
        self.assertLess(covid["benchmark_return"], -0.25)
        hypo = self.a.stress_hypothetical
        eq10 = next(d for d in hypo if d["key"] == "equity_down_10")
        self.assertLess(eq10["total_pnl"], 0)

    def test_liquidity_table(self):
        liq = self.a.liquidity
        self.assertEqual(len(liq), len(self.a.positions))
        self.assertTrue((liq["days_to_liquidate"] >= 0).all())


class OptimizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        close, _, assets, _, _, spec = load_sample(1)
        cls.tickers = list(spec["weights"].keys())
        rets = simple_returns(close[cls.tickers]).iloc[-504:]
        cls.cov = estimate_cov(rets) * TRADING_DAYS
        cls.mu = opt.expected_returns(rets, "shrunk")
        cls.groups = assets["sector"].reindex(cls.tickers)

    def test_min_variance_respects_bounds_and_budget(self):
        cons = opt.Constraints(long_only=True, w_max=0.3)
        r = opt.min_variance(self.cov, cons, self.mu)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(float(r.weights.sum()), 1.0, places=6)
        self.assertLessEqual(float(r.weights.max()), 0.3 + 1e-6)
        self.assertGreaterEqual(float(r.weights.min()), -1e-6)

    def test_max_sharpe_at_least_min_variance_sharpe(self):
        cons = opt.Constraints(long_only=True, w_max=0.35)
        mv = opt.min_variance(self.cov, cons, self.mu, rf=0.04)
        ms = opt.max_sharpe(self.mu, self.cov, cons, rf=0.04)
        self.assertTrue(ms.ok)
        self.assertGreaterEqual(ms.sharpe, mv.sharpe - 1e-6)

    def test_risk_parity_equalises_contributions(self):
        r = opt.risk_parity(self.cov)
        self.assertTrue(r.ok)
        rc = risk_contributions(r.weights, self.cov / TRADING_DAYS)
        self.assertLess(float(rc["pct"].std()), 0.005)

    def test_group_caps_and_turnover(self):
        w0 = pd.Series(1.0 / len(self.tickers), index=self.tickers)
        cons = opt.Constraints(long_only=True, w_max=0.5, groups=self.groups, group_max={"US Large Cap": 0.1},
                               turnover_max=0.2, w0=w0)
        r = opt.max_sharpe(self.mu, self.cov, cons, rf=0.04)
        self.assertTrue(r.ok)
        self.assertLessEqual(float(r.weights["SPY"]), 0.1 + 1e-6)
        self.assertLessEqual(float((r.weights - w0).abs().sum()), 0.2 + 1e-5)

    def test_black_litterman_without_views_returns_prior(self):
        bl = opt.black_litterman(self.cov, pd.Series(1.0, index=self.tickers), [])
        pd.testing.assert_series_equal(bl["prior"], bl["posterior"])

    def test_black_litterman_view_moves_posterior(self):
        bl = opt.black_litterman(self.cov, pd.Series(1.0, index=self.tickers),
                                 [{"assets": {"GLD": 1.0}, "return": 0.30, "confidence": 0.9}])
        self.assertGreater(float(bl["posterior"]["GLD"]), float(bl["prior"]["GLD"]))

    def test_frontier_is_monotone(self):
        pts = opt.efficient_frontier(self.mu, self.cov, opt.Constraints(long_only=True, w_max=0.35), 8)
        vols = [p["vol"] for p in pts]
        rets = [p["return"] for p in pts]
        self.assertEqual(vols, sorted(vols))
        self.assertEqual(rets, sorted(rets))

    def test_vol_target_scale(self):
        w = pd.Series(1.0 / len(self.tickers), index=self.tickers)
        s = opt.vol_target_scale(w, self.cov, 0.10)
        self.assertAlmostEqual(float(np.sqrt(s["scaled_weights"] @ self.cov @ s["scaled_weights"])), 0.10, places=8)


class BacktestTests(unittest.TestCase):
    def test_backtest_runs_and_costs_reduce_return(self):
        close, _, _, _, _, spec = load_sample(1)
        tickers = list(spec["weights"].keys())
        w = pd.Series(spec["weights"])
        free = bt.run_backtest(close[tickers], "current", w, rebalance="M", lookback=252, cost_bps=0)
        costly = bt.run_backtest(close[tickers], "current", w, rebalance="M", lookback=252, cost_bps=50)
        self.assertGreater(len(free.returns), 500)
        self.assertGreater(free.stats["total_return"], costly.stats["total_return"])
        rp = bt.run_backtest(close[tickers], "risk_parity", w, rebalance="Q", lookback=252)
        self.assertTrue(np.isfinite(rp.stats["sharpe"]))
        self.assertAlmostEqual(float(rp.weights.iloc[-1].sum()), 1.0, places=3)


if __name__ == "__main__":
    unittest.main()
