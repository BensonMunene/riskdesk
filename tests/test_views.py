"""Integration tests for the web app and API (loads the sample data into the test database)."""
import json

from django.core.management import call_command
from django.test import TestCase

from core.models import Portfolio, TradeProposal


class WebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_sample_data", verbosity=0)
        cls.p1 = Portfolio.objects.get(name="Flagship Long/Short Equity")
        cls.p2 = Portfolio.objects.get(name="Multi-Asset Balanced")

    def setUp(self):
        self.client.login(username="cio", password="riskdesk")

    def test_login_required(self):
        self.client.logout()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])

    def test_every_portfolio_page_renders(self):
        pk = self.p1.pk
        for path in ("", "positions/", "risk/decomposition/", "risk/factors/", "risk/tail/", "risk/stress/",
                     "risk/liquidity/", "risk/correlation/", "risk/history/", "trades/whatif/", "trades/proposals/",
                     "construct/optimize/", "construct/sizing/", "construct/backtest/", "limits/", "report/", "settings/"):
            r = self.client.get(f"/p/{pk}/{path}")
            self.assertEqual(r.status_code, 200, path)
            self.assertNotIn(b"nan%", r.content, path)

    def test_overview_shows_limit_breach_for_flagship(self):
        r = self.client.get(f"/p/{self.p1.pk}/")
        self.assertContains(r, "breach")
        self.assertContains(r, "risk hog")

    def test_whatif_analyse_and_save(self):
        data = {"ticker[]": ["NVDA", "AMD"], "mode[]": ["shares", "delta_weight"], "value[]": ["-5000", "0.03"],
                "action": "analyse", "name": "", "rationale": ""}
        r = self.client.post(f"/p/{self.p1.pk}/trades/whatif/", data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Before")
        data["action"] = "save"
        data["name"] = "Test proposal"
        r = self.client.post(f"/p/{self.p1.pk}/trades/whatif/", data)
        self.assertEqual(r.status_code, 302)
        prop = TradeProposal.objects.get(name="Test proposal")
        self.assertEqual(prop.lines.count(), 2)
        self.assertIn("verdict", prop.snapshot)

    def test_proposal_apply_books_trades(self):
        data = {"ticker[]": ["NVDA"], "mode[]": ["shares"], "value[]": ["-1000"], "action": "save", "name": "Trim", "rationale": ""}
        self.client.post(f"/p/{self.p1.pk}/trades/whatif/", data)
        prop = TradeProposal.objects.get(name="Trim")
        before = self.p1.positions.get(asset__ticker="NVDA").quantity
        cash_before = self.p1.cash
        self.client.post(f"/p/{self.p1.pk}/trades/proposals/{prop.pk}/status/", {"status": "approved"})
        r = self.client.post(f"/p/{self.p1.pk}/trades/proposals/{prop.pk}/apply/")
        self.assertEqual(r.status_code, 302)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.positions.get(asset__ticker="NVDA").quantity, before - 1000)
        self.assertGreater(self.p1.cash, cash_before)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "executed")

    def test_optimizer_page_runs(self):
        data = {"method": "risk_parity", "return_model": "zero", "universe": "holdings", "candidates": "", "long_only": "on",
                "budget": "1.0", "w_min": "0", "w_max": "0.3", "risk_aversion": "5", "action": "run"}
        r = self.client.post(f"/p/{self.p2.pk}/construct/optimize/", data)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Proposed allocation")
        self.assertEqual(self.p2.optimization_runs.count(), 1)

    def test_positions_import_csv(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv = SimpleUploadedFile("pos.csv", b"ticker,quantity\nAAPL,100\nZZZZ,5\n")
        r = self.client.post(f"/p/{self.p2.pk}/positions/import/", {"file": csv})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.p2.positions.filter(asset__ticker="AAPL", quantity=100).exists())

    def test_exports(self):
        r = self.client.get(f"/p/{self.p1.pk}/export/positions.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"NVDA", r.content)
        r = self.client.get(f"/p/{self.p1.pk}/export/risk.xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.content), 5000)


class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_sample_data", verbosity=0)
        cls.p1 = Portfolio.objects.get(name="Flagship Long/Short Equity")

    def setUp(self):
        self.client.login(username="cio", password="riskdesk")

    def test_api_requires_auth(self):
        self.client.logout()
        r = self.client.get("/api/portfolios/")
        self.assertIn(r.status_code, (401, 403))

    def test_risk_endpoint(self):
        r = self.client.get(f"/api/portfolios/{self.p1.pk}/risk/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertAlmostEqual(sum(p["pct"] for p in body["positions"]), 1.0, places=6)

    def test_whatif_endpoint(self):
        r = self.client.post(f"/api/portfolios/{self.p1.pk}/whatif/",
                             data=json.dumps({"trades": {"NVDA": -5000, "AMD": 8000}}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.json()["comparison"]["verdict"], ("green", "amber", "red"))

    def test_optimize_endpoint(self):
        r = self.client.post(f"/api/portfolios/{self.p1.pk}/optimize/",
                             data=json.dumps({"method": "min_variance", "w_max": 0.1, "long_only": True}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        w = r.json()["weights"]
        self.assertAlmostEqual(sum(w.values()), 1.0, places=4)
