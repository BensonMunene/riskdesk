"""Store today's headline risk metrics for every portfolio (run daily from cron/Task Scheduler).

    python manage.py snapshot_risk
    python manage.py snapshot_risk --backfill 60   # rebuild the last 60 trading days from history
"""
from django.core.management.base import BaseCommand

from core.models import Portfolio, RiskSnapshot
from web.services import build_analyzer, jsonable, load_market_data


class Command(BaseCommand):
    help = "Record headline risk metrics for each portfolio."

    def add_arguments(self, parser):
        parser.add_argument("--backfill", type=int, default=0, help="Number of past trading days to backfill")

    def handle(self, *args, **opts):
        close, volume = load_market_data()
        dates = [close.index[-1]] if not opts["backfill"] else list(close.index[-opts["backfill"]:])
        for port in Portfolio.objects.all():
            n = 0
            for d in dates:
                a = build_analyzer(port, as_of=d)
                s = a.summary()
                metrics = {k: s[k] for k in ("nav", "gross_exposure", "net_exposure", "vol", "beta", "var", "cvar",
                                              "var_dollar", "n_positions", "current_drawdown")}
                metrics["max_risk_share"] = float(a.rc["pct"].max()) if len(a.rc) else None
                metrics["top5"] = a.concentration["top5"]
                RiskSnapshot.objects.update_or_create(portfolio=port, as_of=d.date(), defaults={"metrics": jsonable(metrics)})
                n += 1
            self.stdout.write(f"  {port.name}: {n} snapshot(s)")
        self.stdout.write(self.style.SUCCESS("Done."))
