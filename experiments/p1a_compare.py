"""Summarize measured regression without claiming a causal attribution."""

import json
from pathlib import Path

folder = Path("experiments/reports/p01")
old = json.loads((folder / "final-safety-10000.json").read_text())
new = json.loads((folder / "p1a-final-10000.json").read_text())
old_sql = old["sql_and_transactions"]["execution"]
new_sql = new["sql_and_transactions"]["execution"]
report = {
    "baseline_seconds": old["execution_seconds"],
    "p1a_seconds": new["execution_seconds"],
    "time_ratio": new["execution_seconds"] / old["execution_seconds"],
    "slowdown_percent": 100 * (new["execution_seconds"] / old["execution_seconds"] - 1),
    "same_scenario": old["scenario"] == new["scenario"],
    "same_event_hash": old["replay"]["event_hash"] == new["replay"]["event_hash"],
    "same_state_hash": old["replay"]["state_hash"] == new["replay"]["state_hash"],
    "same_row_counts": old["row_counts"] == new["row_counts"],
    "sql_calls": {"baseline": old_sql["sql_count"], "p1a": new_sql["sql_count"]},
    "sql_seconds": {"baseline": old_sql["sql_seconds"], "p1a": new_sql["sql_seconds"]},
    "execution_memory": {"baseline": old["execution_memory"], "p1a": new["execution_memory"]},
    "audit_memory": {
        "baseline": old["whole_process_memory_after_audits"],
        "p1a": new["whole_process_memory_after_audits"],
    },
    "preexisting_runs": {
        "baseline": old["environment"]["preexisting_runs"],
        "p1a": new["environment"]["preexisting_runs"],
    },
    "representative_sql": [],
    "assessment": "Material wall-time regression observed. Forty extra rail-dispatch reads do not explain its magnitude. Shared host, existing DB size and migration differ. Existing tests overlapped the beginning of the measured run. No controlled causal attribution or large optimization campaign performed; performance acceptance remains HOLD.",
}
for query, measured in list(new_sql["sql"].items())[:8]:
    baseline = old_sql["sql"].get(query)
    report["representative_sql"].append(
        {
            "query": query,
            "baseline": baseline,
            "p1a": measured,
            "time_ratio": measured["seconds"] / baseline["seconds"]
            if baseline and baseline["seconds"]
            else None,
        }
    )
assert all(report[k] for k in ("same_scenario", "same_event_hash", "same_state_hash", "same_row_counts"))
Path("experiments/reports/p1a/performance-comparison.json").write_text(json.dumps(report, indent=2))
print(
    json.dumps(
        {
            k: v
            for k, v in report.items()
            if k not in {"representative_sql", "execution_memory", "audit_memory"}
        }
    )
)
