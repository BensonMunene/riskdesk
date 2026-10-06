"""The weekly_report management command end to end (HTML only; PDF needs a browser)."""
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase


class WeeklyReportCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_sample_data", verbosity=0)

    def test_two_weeks_with_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            call_command("weekly_report", client="data/sample/weekly_client_example.json", date="2026-09-08", out=tmp,
                         no_pdf=True, no_events=True, no_fetch=True, verbosity=0)
            call_command("weekly_report", client="data/sample/weekly_client_example.json", date="2026-09-15", out=tmp,
                         no_pdf=True, no_events=True, no_fetch=True, verbosity=0)
            root = Path(tmp) / "example-client"
            first, second = root / "2026-09-08", root / "2026-09-15"
            for d in (first, second):
                for f in ("report.html", "state.json", "data.json"):
                    self.assertTrue((d / f).exists(), f"{d / f} missing")
            html = (second / "report.html").read_text(encoding="utf-8")
            self.assertIn("vs 2026-09-08", html)
            self.assertIn("Recommended book", html)
            self.assertEqual(html.count('<section class="page">'), 2)
            self.assertNotIn("Traceback", html)
