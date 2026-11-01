"""Bounded OPT-01 timing diagnostic; does not alter financial execution."""

import cProfile
import json
import pstats
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from railstorm.optimizer_experiments import run_policy, specification
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]


def profile(count):
    profiler = cProfile.Profile()
    store = Store()
    started = time.perf_counter()
    profiler.enable()
    result = run_policy(store, specification("OPT-01", seed=20261030, count=count), "fixed-order-v1")
    profiler.disable()
    elapsed = time.perf_counter() - started
    store.engine.dispose()
    stats = pstats.Stats(profiler)
    selected: dict[str, dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "self_seconds": 0.0, "inclusive_seconds": 0.0}
    )
    keys = {
        "reconcile",
        "snapshot",
        "replay_check",
        "_replay_check",
        "execute",
        "evaluate",
        "recover",
        "flush_events",
        "collect",
        "_history",
        "transaction",
        "commit",
        "reduce_event",
        "run_policy",
    }
    for (file, line, name), (primitive, calls, self_s, inclusive_s, callers) in cast(
        Any, stats
    ).stats.items():
        if name not in keys or not ("railstorm" in file or "sqlalchemy" in file):
            continue
        label = f"{Path(file).name}:{name}"
        row = selected[label]
        row["calls"] += calls
        row["self_seconds"] += self_s
        row["inclusive_seconds"] += inclusive_s
    timing = result["timing"]
    return {
        "count": count,
        "elapsed_profiled_seconds": elapsed,
        "run_wall_seconds": timing["wall_seconds"],
        "sql_statement_count": timing["sql_statement_count"],
        "sql_statement_seconds": timing["sql_statement_seconds"],
        "decision_including_database_seconds": timing["decision_including_database_seconds"],
        "algorithm_seconds": timing["algorithm_ns"] / 1e9,
        "completed": result["metrics"]["completed"],
        "reconciliation_errors": result["metrics"]["reconciliation_errors"],
        "duplicate_financial_postings": result["metrics"]["duplicate_financial_postings"],
        "audit_pass": all(
            a["financial_errors"] == 0 and a["replay"]["match"] for a in result["financial_audits"].values()
        )
        and result["routing_audit"]["financial_errors"] == 0,
        "functions": dict(sorted(selected.items())),
        "profiler_note": "Inclusive times overlap; self time does not. cProfile adds overhead. SQL time excludes setup/final audit.",
    }


if __name__ == "__main__":
    rows = []
    for count in (5, 10, 25):
        row = profile(count)
        assert row["completed"] == count and row["audit_pass"]
        rows.append(row)
        (ROOT / "docs/FINAL_bounded_profile.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    k: row[k]
                    for k in (
                        "count",
                        "elapsed_profiled_seconds",
                        "sql_statement_count",
                        "sql_statement_seconds",
                    )
                }
            ),
            flush=True,
        )
