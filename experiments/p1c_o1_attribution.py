"""Exact O0 intent attribution; no new simulation or optimization."""

import json
from collections import Counter
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "docs"
diagnosis = json.loads((root / "P1C_O1_diagnosis.json").read_text())
attribution = []
checks = Counter()
opportunities = Counter()
for row in diagnosis["payments"]:
    if not row["plan"]["primary"]:
        continue
    details = [row[v] for v in ("baseline", "v1")]
    for d in details:
        feasible = [c for c in d["candidates"] if c["status"] == "FEASIBLE"]
        if len(feasible) >= 2:
            opportunities["multiple_eligible_observations"] += 1
            opportunities["differing_fee_observations"] += (
                len({json.dumps(c["fees_by_currency"], sort_keys=True) for c in feasible}) > 1
            )
            opportunities["differing_proceeds_observations"] += (
                len({c["beneficiary_minor"] for c in feasible}) > 1
            )
        requirements = {r["account"]: r["required_minor"] for r in d["estimated_liquidity"]}
        for reservation in d["reservation_events"]:
            if reservation["status"] == "ACTIVE":
                checks["active_reservations_checked"] += 1
                checks["reservation_estimate_mismatches"] += (
                    requirements.get(reservation["account_id"]) != reservation["amount"]
                )
        if d["realized_latency_ms"] is not None:
            checks["completed_fee_estimate_mismatches"] += d["expected_fees"] != d["booked_fees"]
            checks["completed_proceeds_mismatches"] += d["expected_proceeds"] != d["beneficiary_credit"]
        checks["execution_preflight_errors"] += d["execution_error"] is not None
    deltas = {
        c: details[1]["booked_fees"].get(c, 0) - details[0]["booked_fees"].get(c, 0) for c in ("USD", "SGD")
    }
    a, b = [d["realized_latency_ms"] for d in details]
    if row["selection_changed"] or any(deltas.values()) or a != b:
        attribution.append(
            {
                "scenario": row["plan"]["scenario"],
                "seed": row["plan"]["seed"],
                "payment_id": row["intent"]["payment_id"],
                "routes": [d["selected"] for d in details],
                "booked_fees": [d["booked_fees"] for d in details],
                "fee_delta_v1_minus_baseline": deltas,
                "completed_latency_ms": [a, b],
                "beneficiary_credit": [d["beneficiary_credit"] for d in details],
            }
        )
assert checks["completed_fee_estimate_mismatches"] == 0
assert checks["completed_proceeds_mismatches"] == 0
assert checks["reservation_estimate_mismatches"] == 0
totals = {c: sum(r["fee_delta_v1_minus_baseline"][c] for r in attribution) for c in ("USD", "SGD")}
assert totals == {"USD": 1650, "SGD": 1625}
output = {
    "checks": dict(checks),
    "opportunity_observations": dict(opportunities),
    "fee_delta": totals,
    "responsible_intents": attribution,
}
(root / "P1C_O1_attribution.json").write_text(json.dumps(output, indent=2))
lines = [
    "# O0 payment-level fee and latency attribution",
    "",
    "V1 minus baseline; all financial amounts are minor units. A dash in latency means not completed by horizon, never zero latency. Every listed intent is scoped by scenario and seed.",
    "",
    "| Scenario | Seed | Intent | Baseline→V1 | USD fee delta | SGD fee delta | Completed latency baseline/V1 ms |",
    "|---|---:|---|---|---:|---:|---|",
]
for r in attribution:
    latency = "/".join(str(x) if x is not None else "—" for x in r["completed_latency_ms"])
    lines.append(
        f"| {r['scenario']} | {r['seed']} | {r['payment_id']} | {'→'.join(x or 'NO_ROUTE' for x in r['routes'])} | {r['fee_delta_v1_minus_baseline']['USD']} | {r['fee_delta_v1_minus_baseline']['SGD']} | {latency} |"
    )
lines += [
    "",
    f"Exact checks: `{dict(checks)}`.",
    "",
    f"Decision-space observations across both policy histories: `{dict(opportunities)}`. These are110 observations of55 paired intents, not independent samples.",
    "",
    "Account-specific reservation amounts match quotes when actually reserved. This validates magnitude, not end-to-end funding guarantees or future feasibility. Actual reservation lifecycle and obligation histories remain in P1C_O1_diagnosis.json. Fee deltas sum exactly to USD1650 and SGD1625, with currencies kept separate.",
]
(root / "P1C_O1_ATTRIBUTION_APPENDIX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(json.dumps(output["checks"]))
