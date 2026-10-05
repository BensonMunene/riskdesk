"""Tests for the weekly client report: the pure builder (no database) and the management command."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from analytics.report import build_weekly_report, mandate_limits
from analytics.signals import make_trend_rule, stock_snapshot, trend_ls_weights

DATA = Path(__file__).resolve().parents[1] / "data" / "sample"
CFG = Path(__file__).resolve().parents[1] / "data" / "sample" / "weekly_client_example.json"


def load():
    close = pd.read_csv(DATA / "prices_close.csv", index_col=0, parse_dates=True)
    volume = pd.read_csv(DATA / "prices_volume.csv", index_col=0, parse_dates=True)
    meta = pd.read_csv(DATA / "assets.csv").set_index("ticker")
    cfg = json.loads(CFG.read_text())
    return close, volume, meta, cfg


class SignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.close, cls.volume, cls.meta, cls.cfg = load()
        cls.tickers = cls.cfg["tickers"]

    def test_snapshot_fields(self):
        snap = stock_snapshot(self.close, self.volume, self.tickers, "SPY")
        self.assertEqual(len(snap), len(self.tickers))
        self.assertTrue(set(snap["trend"].unique()) <= {"Uptrend", "Downtrend", "Mixed"})
        self.assertTrue(((snap["rsi"] >= 0) & (snap["rsi"] <= 100)).all())
        self.assertTrue((snap["vol"] > 0).all())

    def test_trend_rule_respects_caps_and_target(self):
        from analytics.covariance import ewma_cov
        from analytics.returns import simple_returns
        px = self.close[self.tickers].iloc[-260:]
        cov = ewma_cov(simple_returns(px).iloc[-126:], 30)
        res = trend_ls_weights(px, cov, target_vol=0.15, max_long=0.20, max_short=0.10, gross_max=1.5, max_risk_share=0.25)
        w = res["weights"]
        self.assertLessEqual(float(w.max()), 0.20 + 1e-9)
        self.assertGreaterEqual(float(w.min()), -0.10 - 1e-9)
        self.assertLessEqual(float(w.abs().sum()), 1.5 + 1e-9)
        self.assertLessEqual(res["vol"], 0.15 * 1.05)

    def test_trend_rule_is_stable_week_to_week(self):
        """A name whose target barely moved keeps last week's size."""
        from analytics.covariance import ewma_cov
        from analytics.returns import simple_returns
        px = self.close[self.tickers].iloc[-260:]
        cov = ewma_cov(simple_returns(px).iloc[-126:], 30)
        first = trend_ls_weights(px, cov, max_risk_share=0.25)["weights"]
        again = trend_ls_weights(px, cov, prev=first, turnover_max=0.4, max_risk_share=0.25)
        self.assertLess(float((again["weights"] - first).abs().sum()), 1e-6)

    def test_rule_runs_in_backtest_with_live_weights(self):
        from analytics import backtest as bt
        rule = make_trend_rule(max_risk_share=0.25, long_ma=200, hysteresis=0.05)
        r = bt.run_backtest(self.close[self.tickers], rule, rebalance="W", lookback=252, cost_bps=10, start="2024-09-15")
        self.assertGreater(len(r.returns), 200)
        self.assertLess(r.stats["annual_turnover"], 8.0)   # the stability bands must keep churn well below 800%/yr


class ReportBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.close, cls.volume, cls.meta, cls.cfg = load()
        cls.report = build_weekly_report(cls.cfg, cls.close, cls.volume, cls.meta, prev_state=None, events={}, as_of=pd.Timestamp("2026-09-08"))

    def test_sections_present(self):
        r = self.report
        for key in ("meta", "stocks", "correlation", "construction", "risk", "evidence", "watch", "actions", "commentary", "charts", "state"):
            self.assertIn(key, r)
        self.assertEqual(len(r["stocks"]["rows"]), 10)
        self.assertEqual(len(r["construction"]["objectives"]), 5)
        self.assertTrue(r["meta"]["first_report"])

    def test_house_book_inside_mandate(self):
        risk = self.report["risk"]
        self.assertIsNotNone(risk)
        breaches = [l["label"] for l in risk["limits"] if l["status"] == "breach"]
        self.assertEqual(breaches, [], breaches)
        self.assertLessEqual(max(x["pct"] for x in risk["rc"]), 0.25 + 0.02)

    def test_deltas_on_second_week(self):
        second = build_weekly_report(self.cfg, self.close, self.volume, self.meta, prev_state=self.report["state"],
                                     events={}, as_of=pd.Timestamp("2026-09-15"))
        self.assertFalse(second["meta"]["first_report"])
        self.assertEqual(second["meta"]["prev_date"], "2026-09-08")
        self.assertTrue(all(d["prev"] is not None for d in second["deltas"]["risk"]))
        self.assertLessEqual(second["construction"]["house"]["turnover"], self.cfg["mandate"]["turnover_max_weekly"] + 1e-6)

    def test_mandate_limits_shape(self):
        lims = mandate_limits(self.cfg["mandate"])
        self.assertTrue(all({"metric", "operator", "threshold", "label"} <= set(l) for l in lims))

    def test_report_is_json_serialisable(self):
        json.dumps({k: v for k, v in self.report.items()})


if __name__ == "__main__":
    unittest.main()
