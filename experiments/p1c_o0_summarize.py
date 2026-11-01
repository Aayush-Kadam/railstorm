"""Derive all-cohort summaries from frozen, paired optimizer experiment artifacts."""

import json
import math
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "docs"
cohort = json.loads((root / "P1C_O0_results.json").read_text())
assert cohort["status"] == "COMPLETE" and len(cohort["runs"]) == 12
rows = cohort["runs"]
summary = {}
for group, selected in [
    ("all_planned", rows),
    ("supported_primary", [r for r in rows if r["plan"]["primary"]]),
    ("held_out_supported", [r for r in rows if r["plan"]["primary"] and r["plan"]["split"] == "held-out"]),
]:
    stats = []
    for index in (0, 1):
        policies = [r["comparison"]["policies"][index] for r in selected]
        metrics = [p["metrics"] for p in policies]
        value = {
            k: sum(m[k] for m in metrics)
            for k in [
                "submitted",
                "eligible",
                "completed",
                "rejected",
                "safely_failed",
                "pending",
                "reconciliation_errors",
                "duplicate_financial_postings",
                "recovery_attempts",
            ]
        }
        for name in ("fees_by_currency", "beneficiary_credited_by_currency"):
            value[name] = {c: sum(m[name].get(c, 0) for m in metrics) for c in ("USD", "SGD")}
        lat = []
        for policy in policies:
            for outcome in policy["outcomes"].values():
                state = outcome["payment"]
                if state and state["economic_status"] == "SETTLED":
                    lat.append(
                        state.get("settled_ms", state.get("destination_final_ms"))
                        - outcome["receipt"]["arrival_ms"]
                    )
        lat.sort()
        value["pooled_completed_latency_ms"] = {
            f"p{q}": lat[math.ceil(len(lat) * q / 100) - 1] if lat else None for q in (50, 95, 99)
        }
        value["latency_sample_size"] = len(lat)
        stats.append(value)
    summary[group] = {"fixed_order": stats[0], "liquidity_aware": stats[1]}
deltas = [
    r["comparison"]["policies"][1]["metrics"]["completed"]
    - r["comparison"]["policies"][0]["metrics"]["completed"]
    for r in rows
    if r["plan"]["primary"]
]
assert all(r["comparison"]["fairness_checked"] for r in rows)
assert all(p["metrics"]["reconciliation_errors"] == 0 for r in rows for p in r["comparison"]["policies"])
if not any(d > 0 for d in deltas):
    conclusion = "NO ADVANTAGE DEMONSTRATED"
elif any(d < 0 for d in deltas):
    conclusion = "MIXED TRADEOFFS"
else:
    conclusion = "EVIDENCE INCONCLUSIVE"  # Additional cost/exposure checks required before promoting a completion gain.
summary["conclusion"] = conclusion
summary["method"] = (
    "Declared primary outcome is financial completion by horizon; all currency-specific fee, latency and funding tradeoffs remain reported. No global optimality/generalization claim."
)
(root / "P1C_O0_summary.json").write_text(json.dumps(summary, indent=2))
print(conclusion)
