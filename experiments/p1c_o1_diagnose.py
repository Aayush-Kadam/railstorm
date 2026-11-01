"""Read-only O0 forensics and fresh-world reproduction, before policy redesign."""

import json
from collections import Counter
from pathlib import Path

from railstorm.optimizer_experiments import _history, run_policy
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]


def detail(store, result, decision, histories=None):
    key = decision["intent"]["payment_id"]
    selected = decision["selected"]
    outcome = result["outcomes"][key]
    state = outcome["payment"]
    candidate = next((c for c in decision["candidates"] if c["rail"] == selected), None)
    fees = Counter()
    journals, reservations, obligations = [], [], []
    if selected:
        events = histories[selected] if histories else _history(store, result["physical_runs"][selected])
        for event in events:
            p = event["payload"]
            if event["event_type"] == "JOURNAL_POSTED" and p.get("payment_id") == key:
                journals.append({"at_ms": event["logical_time_ms"], **p})
                for line in p["lines"]:
                    a = line["account_id"]
                    if a.startswith("OPERATOR:") or (a.startswith("GL:") and ":FEES:" in a):
                        fees[p["currency"]] += line["delta"]
            if event["event_type"] == "RESERVATION_CHANGED" and p.get("payment_id") == key:
                reservations.append({"at_ms": event["logical_time_ms"], **p})
            if event["event_type"] == "CB_OBLIGATION_CHANGED" and p.get("payment_id") == key:
                obligations.append({"at_ms": event["logical_time_ms"], **p})
            if event["event_type"] == "PAYMENT_CHANGED" and p.get("id") == key and p.get("obligation"):
                obligations.append({"at_ms": event["logical_time_ms"], **p["obligation"]})
    completed = bool(state and state["economic_status"] == "SETTLED")
    credit = (
        (state["beneficiary_minor"] if selected == "CORRESPONDENT" else state["quote"]["output_minor"])
        if completed
        else 0
    )
    return {
        "selected": selected,
        "eligible_routes": [c["rail"] for c in decision["candidates"] if c["status"] == "FEASIBLE"],
        "candidates": decision["candidates"],
        "expected_fees": candidate["fees_by_currency"] if candidate else {},
        "booked_fees": dict(fees),
        "expected_proceeds": candidate["beneficiary_minor"] if candidate else None,
        "beneficiary_credit": credit,
        "estimated_liquidity": candidate["liquidity"] if candidate else [],
        "reservation_events": reservations,
        "realized_journals": journals,
        "obligation_history": obligations,
        "final_state": state,
        "execution_error": outcome["receipt"]["execution_error"],
        "modeled_candidate_latency_ms": candidate["latency_ms"] if candidate else None,
        "realized_latency_ms": (state.get("settled_ms", state.get("destination_final_ms")))
        - outcome["receipt"]["arrival_ms"]
        if completed
        else None,
        "reconciliation_errors": result["metrics"]["reconciliation_errors"],
    }


def main():
    original = json.loads((ROOT / "docs/P1C_O0_results.json").read_text())
    store = Store()
    rows, reproductions = [], []
    for entry in original["runs"]:
        pair = entry["comparison"]
        for result in pair["policies"]:
            fresh = run_policy(store, pair["specification"], result["policy"]["version"])
            matched = fresh["canonical_result_hash"] == result["canonical_result_hash"]
            reproductions.append(
                {
                    "plan": entry["plan"],
                    "policy": result["policy"]["version"],
                    "matched": matched,
                    "original_hash": result["canonical_result_hash"],
                    "fresh_hash": fresh["canonical_result_hash"],
                }
            )
            assert matched, "GATE A STOP: frozen outcome not reproduced"
        baseline, v1 = pair["policies"]
        for a, b in zip(baseline["decisions"], v1["decisions"], strict=True):
            assert a["intent"] == b["intent"]
            rows.append(
                {
                    "plan": entry["plan"],
                    "intent": a["intent"],
                    "selection_changed": a["selected"] != b["selected"],
                    "baseline": detail(store, baseline, a),
                    "v1": detail(store, v1, b),
                }
            )
        print("Reproduced", entry["plan"], flush=True)
    primary = [r for r in rows if r["plan"]["primary"]]
    stats = {}
    for version in ("baseline", "v1"):
        fees = Counter()
        completed = []
        for row in primary:
            fees.update(row[version]["booked_fees"])
            if row[version]["realized_latency_ms"] is not None:
                completed.append(row[version]["realized_latency_ms"])
        stats[version] = {
            "completed": len(completed),
            "fees": dict(fees),
            "completed_ids": [
                f"{r['plan']['scenario']}/{r['plan']['seed']}/{r['intent']['payment_id']}"
                for r in primary
                if r[version]["realized_latency_ms"] is not None
            ],
        }
    stats["route_changes"] = sum(r["selection_changed"] for r in primary)
    stats["multi_eligible_decisions"] = {
        v: sum(len(r[v]["eligible_routes"]) >= 2 for r in primary) for v in ("baseline", "v1")
    }
    output = {
        "gate_a": "PASS",
        "assigned": 120,
        "supported_assigned": 110,
        "proxy_assigned": 10,
        "comparable_intents": len(rows),
        "primary_pairs": len(primary),
        "reproductions": reproductions,
        "summary": stats,
        "payments": rows,
    }
    (ROOT / "docs/P1C_O1_diagnosis.json").write_text(json.dumps(output, indent=2))
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
