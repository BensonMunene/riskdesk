"""Dump the key numbers behind each page so the user guide can quote them exactly.

    python scripts/dump_guide_numbers.py > docs/guide/numbers.json
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from core.models import Portfolio  # noqa: E402
from web.services import build_analyzer, df_records, jsonable, limits_for  # noqa: E402

out = {}
for p in Portfolio.objects.all():
    a = build_analyzer(p)
    lim = a.check_limits(limits_for(p))
    fd = a.factor_decomposition
    out[p.name] = {
        "summary": jsonable(a.summary()),
        "performance": jsonable(a.performance),
        "rc": df_records(a.rc.round(4)),
        "sector_rc": df_records(a.sector_rc.round(4), "sector"),
        "sector_weights": df_records(a.sector_weights.round(4), "sector"),
        "factor_exposures": df_records(fd["exposures"].round(4), "factor"),
        "factor_split": {k: jsonable(fd[k]) for k in ("systematic_vol", "idiosyncratic_vol", "total_vol", "systematic_share")},
        "var_table": [jsonable(v.as_dict()) for v in a.var_table],
        "var_contrib": df_records(a.var_contrib.head(10).round(5)),
        "drawdowns": df_records(a.drawdowns, "episode"),
        "worst_windows": df_records(a.worst_windows, "rank"),
        "stress_hist": [{k: jsonable(v) for k, v in d.items() if k in ("name", "start", "end", "total_pnl", "total_dollar", "benchmark_return")} for d in a.stress_historical if d.get("available")],
        "stress_hypo": [{k: jsonable(v) for k, v in d.items() if k in ("name", "shocks", "total_pnl", "total_dollar")} for d in a.stress_hypothetical],
        "single_name_shocks": df_records(a.single_name_shocks.head(5).round(5)),
        "concentration": jsonable(a.concentration),
        "clusters": jsonable(a.clusters[:5]),
        "liquidity_profile": df_records(a.liquidity_profile.round(4), "bucket"),
        "liquidity_top": df_records(a.liquidity.head(5).round(4)),
        "limits": jsonable(lim),
        "insights": jsonable([i.as_dict() for i in a.insights(lim)]),
        "warnings": a.all_warnings(),
    }
print(json.dumps(out, indent=1, default=str))
