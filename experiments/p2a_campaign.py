"""Deterministic, bounded synthetic chaos evidence from native rail worlds."""

import argparse
import ctypes
import json
import time
import tracemalloc
import uuid
from pathlib import Path

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand, digest
from railstorm.instant_fixtures import execute_fixture
from railstorm.ledger import Ledger
from railstorm.optimizer_experiments import run_policy, specification
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.instant_link import InstantLink
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "p2_a_evidence"
REGISTRY = {
    "CH-01": ("healthy mixed routing", "optimizer", "OPT-07"),
    "CH-02": ("destination FX liquidity shortage", "optimizer", "OPT-02"),
    "CH-03": ("correspondent intermediary outage", "correspondent", "CB-R06"),
    "CH-04": ("instant destination outage and recovery", "instant", "outage"),
    "CH-05": ("atomic participant outage before commit", "atomic", "outage"),
    "CH-06": ("lost instant acknowledgement", "instant", "lost_ack"),
    "CH-07": ("duplicate instant commands", "instant", "duplicate"),
    "CH-08": ("liquidity contention", "optimizer", "OPT-08"),
    "CH-09": ("restart during instant recovery", "instant", "restart"),
    "CH-10": ("quote expiry", "instant", "expiry"),
    "CH-11": ("compounded freeze, participant outage and retry", "optimizer", "OPT-10"),
    "CH-12": ("event corruption negative control", "corruption", "event"),
}


def _peak_working_set():
    if not hasattr(ctypes, "windll"):
        return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
                "PrivateUsage",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
    if ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        return counters.PeakWorkingSetSize
    return None


def _native(store, run):
    start = time.perf_counter()
    independent = independent_audit(store, run)
    audit_seconds = time.perf_counter() - start
    reconciliation = reconcile(store, run)
    start = time.perf_counter()
    replay = replay_check(store, run)
    return {
        "independent": independent,
        "reconciliation": reconciliation,
        "replay": replay,
        "independent_audit_seconds": audit_seconds,
        "replay_seconds": time.perf_counter() - start,
    }


def _capture(store, run):
    from sqlalchemy import text

    with store.engine.connect() as conn:

        def rows(sql):
            return [dict(row) for row in conn.execute(text(sql), {"r": run}).mappings()]

        return {
            "stored_row_bytes": sum(
                conn.execute(
                    text(f"SELECT coalesce(sum(pg_column_size(t)),0) FROM {table} t WHERE run_id=:r"),
                    {"r": run},
                ).scalar_one()
                for table in (
                    "events",
                    "journal_entries",
                    "journal_lines",
                    "positions",
                    "payments",
                    "reservations",
                    "cb_obligations",
                    "il_messages",
                )
            ),
            "opening_journals": rows(
                "SELECT e.id,e.currency,l.account_id,l.delta FROM journal_entries e JOIN journal_lines l ON l.run_id=e.run_id AND l.entry_id=e.id WHERE e.run_id=:r AND e.payment_id IS NULL ORDER BY e.id,l.line_no"
            ),
            "positions": rows(
                "SELECT p.account_id,a.currency,a.owner,a.issuer,p.posted,p.reserved,p.buffer FROM positions p JOIN accounts a ON a.run_id=p.run_id AND a.id=p.account_id WHERE p.run_id=:r ORDER BY p.account_id"
            ),
            "payments": rows("SELECT id,data FROM payments WHERE run_id=:r ORDER BY id"),
            "reservations": rows(
                "SELECT id,payment_id,account_id,amount,status FROM reservations WHERE run_id=:r ORDER BY id"
            ),
            "correspondent_obligations": rows(
                "SELECT id,payment_id,amount,currency,status FROM cb_obligations WHERE run_id=:r ORDER BY id"
            ),
            "messages": rows("SELECT id,data FROM il_messages WHERE run_id=:r ORDER BY id"),
            "routing_records": rows("SELECT id,data FROM routing_records WHERE run_id=:r ORDER BY id"),
        }


def _instant(store, mode):
    run = "p2a-" + uuid.uuid4().hex
    rail = InstantLink(store)
    rail.initialize(run, provider_sgd=50000)
    command = PaymentCommand(idempotency_key="payment", principal_minor=10000, requested_rail="INSTANT_LINK")
    stages = []

    def record(at_ms, action, receipt=None, audit=None):
        stage = {"at_ms": at_ms, "action": action, "payment": rail.inspect(run, "payment")["payment"]}
        if receipt is not None:
            stage["receipt"] = receipt
        if audit is not None:
            stage["audit"] = audit
        stages.append(stage)

    record(0, "prepare", rail.prepare(run, command))
    if mode == "expiry":
        record(30000, "expire", rail.deliver(run, "payment", "SOURCE", 30000))
    else:
        record(10, "source", rail.deliver(run, "payment", "SOURCE", 10))
        if mode in ("outage", "restart"):
            record(
                30,
                "destination_unavailable",
                rail.deliver(run, "payment", "DESTINATION", 30, available=False),
            )
            record(30, "pending_audit", audit=_native(store, run))
            restarted = InstantLink(Store()) if mode == "restart" else rail
            try:
                record(40, "recover", restarted.recover(run, "payment", 40))
                record(41, "duplicate_recover", restarted.recover(run, "payment", 41))
            finally:
                if restarted is not rail:
                    restarted.store.engine.dispose()
        else:
            record(
                30,
                "destination",
                rail.deliver(run, "payment", "DESTINATION", 30, lose_ack=mode == "lost_ack"),
            )
            if mode == "lost_ack":
                record(40, "recover_ack", rail.recover(run, "payment", 40))
            if mode == "duplicate":
                record(40, "duplicate_prepare", rail.prepare(run, command))
                record(41, "duplicate_deliver", rail.deliver(run, "payment", "DESTINATION", 41))
    return {"runs": [run], "stages": stages, "final": rail.inspect(run, "payment")}


def _atomic(store):
    from railstorm.scenarios import Fault
    from railstorm.simulation import apply_fault

    run = "p2a-" + uuid.uuid4().hex
    rail = Ledger(store)
    rail.initialize(run)
    cmd = PaymentCommand(idempotency_key="payment")
    stages = [{"at_ms": 0, "action": "prepare", "payment": rail.prepare(run, cmd)}]
    fault = Fault(id="bank-down", type="PARTICIPANT_DOWN", target="STERLING_UK", at_ms=1, duration_ms=9)
    apply_fault(store, run, fault, True, 1)
    stages.append({"at_ms": 2, "action": "blocked_settle", "payment": rail.settle(run, "payment", 2)})
    stages.append({"at_ms": 2, "action": "prepared_audit", "audit": _native(store, run)})
    apply_fault(store, run, fault, False, 10)
    stages.append({"at_ms": 11, "action": "settle", "payment": rail.settle(run, "payment", 11)})
    stages.append({"at_ms": 12, "action": "duplicate_settle", "payment": rail.settle(run, "payment", 12)})
    rail.acknowledge(run, "payment")
    with store.transaction(run) as tx:
        final = tx.payment("payment")
    return {"runs": [run], "stages": stages, "final": final}


def _corruption(store):
    fixture = execute_fixture(store, "IL-01")
    run = fixture["run_id"]
    with store.engine.connect() as conn:
        from sqlalchemy import text

        row = dict(
            conn.execute(text("SELECT * FROM events WHERE run_id=:r ORDER BY seq LIMIT 1"), {"r": run})
            .mappings()
            .one()
        )
    tampered = dict(row)
    tampered["event_type"] = "INTENTIONALLY_CORRUPTED"
    value = {k: v for k, v in tampered.items() if k not in ("run_id", "event_hash")}
    import hashlib

    recalculated = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    assert recalculated != tampered["event_hash"]
    return {
        "runs": [run],
        "corruption": {
            "mode": "isolated copied event fixture",
            "detected": True,
            "original_event_hash": row["event_hash"],
            "tampered_event_hash": recalculated,
        },
        "stages": [],
        "final": fixture["inspection"],
    }


def _correspondent(store):
    from railstorm.correspondent_routing_examples import example, restore
    from railstorm.routing import Router

    case = example(store, "CB-R06")
    control = case["control_run"]
    world = case["status"]["world"]
    stages = [
        {
            "at_ms": 200,
            "action": "intermediary_unavailable",
            "status": case["status"],
            "audit": _native(store, world),
        }
    ]
    stages.append({"at_ms": 800, "action": "restore", "status": restore(store, control, "payment", 800)})
    router = Router(store)
    for moment in range(900, 2500, 100):
        outcome = router.recover(control, "payment", moment)
        if outcome["payment"]["economic_status"] == "SETTLED":
            break
    stages.append({"at_ms": moment, "action": "recover", "status": router.status(control, "payment")})
    return {"runs": [control, world], "stages": stages, "final": router.status(control, "payment")}


def execute(store, scenario, seed=20261030, count=10):
    if scenario not in REGISTRY:
        raise ValueError("UNKNOWN_CHAOS_SCENARIO")
    name, kind, variant = REGISTRY[scenario]
    started = time.perf_counter()
    trace_allocations = count <= 10
    if trace_allocations:
        tracemalloc.start()
    if kind == "optimizer":
        spec = specification(variant, seed, count)
        if scenario == "CH-11":
            spec["target_funding_minor"] = 20000
            spec["cb_target_funding_minor"] = 100
            spec["faults"].append(
                {
                    "kind": "PARTICIPANT_DOWN",
                    "id": "sg-outage",
                    "bank": "MERIDIAN_SG",
                    "start": 80,
                    "end": 240,
                }
            )
        native = run_policy(store, spec, "fixed-order-v1")
        runs = list(native["physical_runs"].values())
        payload = {
            "specification": spec,
            "native": native,
            "runs": runs,
            "stages": [],
            "final": native["outcomes"],
        }
    elif kind == "correspondent":
        payload = _correspondent(store)
    elif kind == "instant":
        payload = _instant(store, variant)
    elif kind == "atomic":
        payload = _atomic(store)
    else:
        payload = _corruption(store)
    audit = {run: _native(store, run) for run in payload["runs"]}
    durable = {run: _capture(store, run) for run in payload["runs"]}
    if any(
        not v["independent"]["pass"] or v["reconciliation"]["financial_errors"] or not v["replay"]["match"]
        for v in audit.values()
    ):
        raise RuntimeError("CHAOS_FINANCIAL_SAFETY_FAILURE")
    if any(not s["audit"]["independent"]["pass"] for s in payload["stages"] if "audit" in s):
        raise RuntimeError("PENDING_STAGE_SAFETY_FAILURE")
    manifest = {
        "scenario_id": scenario,
        "configuration_version": "p2-a-1",
        "seed": seed,
        "name": name,
        "requested_count": count if kind == "optimizer" else 1,
        "routing_policy": "FIXED_ORDER",
        "variant": variant,
    }
    peak_memory = None
    if trace_allocations:
        _, peak_memory = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return {
        "status": "PASS",
        "manifest": manifest,
        "manifest_hash": digest(manifest),
        "workload_hash": native["workload_hash"] if kind == "optimizer" else digest(manifest),
        "initial_state_hash": native["initial_state_hash"]
        if kind == "optimizer"
        else digest([value["opening_journals"] for value in durable.values()]),
        "fault_schedule_hash": native["fault_schedule_hash"] if kind == "optimizer" else digest(variant),
        "payload": payload,
        "audit": audit,
        "durable_financial_state": durable,
        "canonical_event_history_hash": digest(
            sorted(value["independent"]["event_hash"] for value in audit.values())
        ),
        "final_financial_state_hash": digest(
            sorted(value["independent"]["financial_state_hash"] for value in audit.values())
        ),
        "runtime_seconds": time.perf_counter() - started,
        "peak_traced_python_bytes": peak_memory,
        "peak_process_working_set_bytes": _peak_working_set(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", nargs="*", default=list(REGISTRY))
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    store = Store()
    for scenario in args.scenarios:
        result = execute(store, scenario, args.seed, args.count)
        path = OUT / f"{scenario}-seed{args.seed}-n{args.count}.json"
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(scenario, result["status"], round(result["runtime_seconds"], 3), "s", flush=True)


if __name__ == "__main__":
    main()
