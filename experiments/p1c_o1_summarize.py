"""Outcome-neutral reconstruction of all O1 aggregates and paired comparable costs."""

import json
import math
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "docs"


def aggregate(groups, index):
    results = [g["policies"][index] for g in groups]
    sums = {
        key: sum(r["metrics"][key] for r in results)
        for key in (
            "submitted",
            "eligible",
            "completed",
            "pending",
            "rejected",
            "safely_failed",
            "recovery_attempts",
            "reconciliation_errors",
            "duplicate_financial_postings",
        )
    }
    for key in ("fees_by_currency", "beneficiary_credited_by_currency", "obligation_amount_time_minor_ms"):
        sums[key] = dict(sum((Counter(r["metrics"][key]) for r in results), Counter()))
    for key in ("peak_reserved_by_currency", "peak_reserved_by_account", "peak_obligations_by_currency"):
        values = [r["metrics"][key] for r in results]
        sums["max_across_scenarios_" + key] = {
            c: max(v.get(c, 0) for v in values)
            for c in sorted(set().union(*values))
            if any(v.get(c, 0) for v in values)
        }
    sums["reservation_amount_time_minor_ms"] = dict(
        sum((Counter(r["reservation_amount_time_minor_ms"]) for r in results), Counter())
    )
    sums["routes"] = dict(sum((Counter(r["route_distribution"]) for r in results), Counter()))
    latencies = sorted(
        d["realized_latency_ms"]
        for r in results
        for d in r["per_payment"]
        if d["realized_latency_ms"] is not None
    )
    sums["latency_ms"] = {
        f"p{q}": latencies[math.ceil(len(latencies) * q / 100) - 1] if latencies else None
        for q in (50, 95, 99)
    }
    sums["latency_n"] = len(latencies)
    sums["success_rate"] = {"numerator": sums["completed"], "denominator": sums["submitted"]}
    fees_completed = Counter()
    for r in results:
        for d in r["per_payment"]:
            if d["realized_latency_ms"] is not None:
                fees_completed.update(d["booked_fees"])
    sums["fees_per_completion"] = {
        c: {"numerator": n, "denominator": len(latencies)} for c, n in fees_completed.items()
    }
    sums["algorithm_ns"] = sum(r["timing"]["algorithm_ns"] for r in results)
    sums["client_wall_seconds"] = sum(r["timing"]["wall_seconds"] for r in results)
    return sums


def main():
    cohort = json.loads((ROOT / "P1C_O1_primary_v2_results.json").read_text())
    assert cohort["status"] == "COMPLETE" and len(cohort["runs"]) == 10
    groups = cohort["runs"]
    summary = {"policies": {g: aggregate(groups, i) for i, g in enumerate(("fixed_order", "v1", "v2"))}}
    pairs = []
    payment_path = ROOT / "P1C_O1_heldout_payments.jsonl"
    payment_rows = []
    for group in groups:
        baseline, v1, v2 = group["policies"]
        payments = []
        for index, demand in enumerate(group["plan"]["specification"]["workload"]):
            details = [r["per_payment"][index] for r in (baseline, v1, v2)]
            assert all(
                r["decisions"][index]["intent"]["payment_id"] == demand["id"] for r in (baseline, v1, v2)
            )
            payments.append(
                {
                    "intent": demand,
                    "details": details,
                    "changed_baseline_v2": details[0]["selected"] != details[2]["selected"],
                }
            )
            for policy_index, detail in enumerate(details):
                payment_rows.append(
                    {
                        "family": group["plan"]["family"],
                        "seed": group["plan"]["seed"],
                        "intent": demand,
                        "policy": ("fixed_order", "v1", "v2")[policy_index],
                        "observation": detail,
                        "actual_liquidity": actual_liquidity(
                            detail, group["plan"]["specification"]["horizon_ms"]
                        ),
                    }
                )
        common_costs = []
        for other in (0, 1):
            common = [
                p
                for p in payments
                if all(p["details"][i]["realized_latency_ms"] is not None for i in (other, 2))
            ]
            common_costs.append(
                {
                    "other_policy": ("fixed_order", "v1")[other],
                    "common_completed": len(common),
                    "fees": [
                        dict(sum((Counter(p["details"][i]["booked_fees"]) for p in common), Counter()))
                        for i in (other, 2)
                    ],
                    "proceeds": [
                        {
                            c: sum(
                                p["details"][i]["beneficiary_credit"]
                                for p in common
                                if p["intent"]["target"] == c
                            )
                            for c in ("USD", "SGD")
                        }
                        for i in (other, 2)
                    ],
                }
            )
        pairs.append(
            {
                "plan": {k: v for k, v in group["plan"].items() if k != "specification"},
                "metrics": [r["metrics"] for r in group["policies"]],
                "common_completed_costs": common_costs,
                "route_changes_baseline_v2": sum(p["changed_baseline_v2"] for p in payments),
                "different_completed_ids": [
                    p["intent"]["id"]
                    for p in payments
                    if (p["details"][0]["realized_latency_ms"] is None)
                    != (p["details"][2]["realized_latency_ms"] is None)
                ],
            }
        )
    summary["scenarios"] = pairs
    payment_path.write_text("".join(json.dumps(row) + "\n" for row in payment_rows))
    summary["fairness_passed"] = all(g["fairness_checked"] for g in groups)
    summary["source_freeze"] = json.loads((ROOT / "P1C_O1_frozen_v2.json").read_text())
    (ROOT / "P1C_O1_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary["policies"], indent=2))
    ablation = json.loads((ROOT / "P1C_O1_ablation_v2_results.json").read_text())
    assert ablation["status"] == "COMPLETE"
    (ROOT / "P1C_O1_ablation_summary.json").write_text(
        json.dumps(
            {
                mode: aggregate(ablation["runs"], i)
                for i, mode in enumerate(("cost_only", "exposure_only", "full"))
            },
            indent=2,
        )
    )


def actual_liquidity(detail, horizon):
    currencies = {r["account"]: r["currency"] for r in detail["estimated_liquidity"]}
    accounts = {}
    active = set()
    for event in detail["reservation_events"]:
        account = event["account_id"]
        state = accounts.setdefault(
            account,
            {
                "currency": currencies.get(account),
                "active_minor": 0,
                "peak_active_minor": 0,
                "consumed_minor": 0,
                "amount_time_minor_ms": 0,
                "last_ms": 0,
            },
        )
        at = event["at_ms"]
        state["amount_time_minor_ms"] += state["active_minor"] * (at - state["last_ms"])
        state["last_ms"] = at
        if event["status"] == "ACTIVE":
            assert event["id"] not in active
            active.add(event["id"])
            state["active_minor"] += event["amount"]
            state["peak_active_minor"] = max(state["peak_active_minor"], state["active_minor"])
        elif event["id"] in active:
            active.remove(event["id"])
            state["active_minor"] -= event["amount"]
            if event["status"] == "CONSUMED":
                state["consumed_minor"] += event["amount"]
    for state in accounts.values():
        state["amount_time_minor_ms"] += state["active_minor"] * (horizon - state.pop("last_ms"))
    return accounts


if __name__ == "__main__":
    main()
