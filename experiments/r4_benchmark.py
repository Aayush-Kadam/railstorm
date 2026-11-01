"""Matched hashing/audit and frozen native workload ladder, bounded externally."""

import argparse
import importlib.util
import json
import statistics
import time
import uuid
from pathlib import Path

from sqlalchemy import event

import railstorm.audit as audit
from experiments.p2a_campaign import _peak_working_set
from experiments.r4_profile import resources
from railstorm.domain import digest
from railstorm.optimizer_experiments import run_policy
from railstorm.p2a_oracle import independent_audit
from railstorm.scenarios import Scenario
from railstorm.simulation import create_run, metrics, step
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r4_evidence"


def baseline():
    spec = importlib.util.spec_from_file_location("r4_frozen_audit", OUT / "baseline_audit.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def baseline_store():
    spec = importlib.util.spec_from_file_location("r4_frozen_store", OUT / "baseline_store.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Store()


def compare():
    old = baseline()
    original = audit.state_digest
    manifest = json.loads((OUT / "contract.json").read_text())
    spec = manifest["secondary_mixed"]
    rows = []
    for label in ("baseline", "modified", "modified", "baseline"):
        store = baseline_store() if label == "baseline" else Store()
        audit.state_digest = old.state_digest if label == "baseline" else original
        cpu = time.process_time()
        result = run_policy(store, spec, "fixed-order-v1")
        cpu = time.process_time() - cpu
        worlds = result["physical_runs"]
        differential = {}
        for rail, run in worlds.items():
            previous = old.reconcile(store, run)
            current = audit.reconcile(store, run)
            assert previous == current
            assert previous["financial_errors"] == 0 and previous["replay"]["match"]
            independent = independent_audit(store, run)
            assert independent["pass"]
            differential[rail] = {"old": previous, "new": current, "independent": independent}
        rows.append(
            {
                "label": label,
                "result": result,
                "differential": differential,
                "client_cpu_seconds": cpu,
                "peak_client_bytes": _peak_working_set(),
            }
        )
        (OUT / "matched_mixed.json").write_text(json.dumps(rows, indent=2))
        print(
            json.dumps(
                {
                    "label": label,
                    "wall": result["timing"]["wall_seconds"],
                    "hash": result["canonical_result_hash"],
                }
            ),
            flush=True,
        )
    audit.state_digest = original
    # Control and financial world UUIDs are normalized by the existing comparison harness.
    assert len({r["result"]["canonical_result_hash"] for r in rows}) == 1
    assert len({r["result"]["input_hash"] for r in rows}) == 1
    print(
        json.dumps(
            {
                label: statistics.median(
                    r["result"]["timing"]["wall_seconds"] for r in rows if r["label"] == label
                )
                for label in ("baseline", "modified")
            }
        ),
        flush=True,
    )


def atomic(count, implementation="modified"):
    store = baseline_store() if implementation == "baseline" else Store()
    old = baseline()
    spec = Scenario.model_validate_json((ROOT / "scenarios/stress_10000.json").read_text()).model_copy(
        update={"payment_count": count}
    )
    run = "r4-atomic-" + uuid.uuid4().hex
    (OUT / f"atomic-{count}-{implementation}-attempt.json").write_text(
        json.dumps({"run_id": run, "count": count, "status": "STARTED", "resources": resources()}, indent=2)
    )
    print(json.dumps({"started_run": run, "count": count}), flush=True)
    sql = {"count": 0, "seconds": 0.0, "logical_executions": 0}

    def before(conn, cursor, statement, parameters, context, executemany):
        context.r4_started = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, executemany):
        sql["count"] += 1
        sql["logical_executions"] += len(parameters) if executemany else 1
        sql["seconds"] += time.perf_counter() - context.r4_started

    event.listen(store.engine, "before_cursor_execute", before)
    event.listen(store.engine, "after_cursor_execute", after)
    before_resources = resources()
    cpu = time.process_time()
    started = time.perf_counter()
    create_run(store, run, spec)
    while not step(store, run, 1000)["done"]:
        pass
    execution = time.perf_counter() - started
    cpu = time.process_time() - cpu
    execution_sql = dict(sql)
    started = time.perf_counter()
    native = audit.reconcile(store, run)
    audit_s = time.perf_counter() - started
    started = time.perf_counter()
    replay = audit.replay_check(store, run)
    replay_s = time.perf_counter() - started
    started = time.perf_counter()
    independent = independent_audit(store, run)
    independent_s = time.perf_counter() - started
    previous = old.reconcile(store, run)
    assert previous == native and native["financial_errors"] == 0 and replay["match"] and independent["pass"]
    measurements = metrics(store, run)
    assert measurements["settled"] == count
    with store.transaction(run) as tx:
        state = audit.snapshot(tx)
        journals = tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r")["n"]
        events = tx.one("SELECT count(*) n FROM events WHERE run_id=:r")["n"]
    report = {
        "implementation": implementation,
        "count": count,
        "run_id": run,
        "workload": spec.model_dump(),
        "workload_hash": digest(spec.model_dump()),
        "execution_seconds": execution,
        "payments_per_second": count / execution,
        "client_cpu_seconds": cpu,
        "client_mean_cpu_one_core_percent": 100 * cpu / execution,
        "native_audit_seconds": audit_s,
        "standalone_replay_seconds": replay_s,
        "independent_seconds": independent_s,
        "execution_sql": execution_sql,
        "all_sql": sql,
        "metrics": measurements,
        "native": native,
        "replay": replay,
        "independent": independent,
        "baseline_audit_identical": previous == native,
        "journal_count": journals,
        "event_count": events,
        "state_hash": audit.state_digest(state),
        "peak_client_bytes": _peak_working_set(),
        "resources_before": before_resources,
        "resources_after": resources(),
        "memory_scope": "client process high-water; PostgreSQL server excluded; CPU client only",
    }
    (OUT / f"atomic-{count}-{implementation}.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "count",
                    "execution_seconds",
                    "payments_per_second",
                    "native_audit_seconds",
                    "peak_client_bytes",
                )
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("compare", "atomic"))
    parser.add_argument("--implementation", choices=("baseline", "modified"), default="modified")
    parser.add_argument("--count", type=int, default=25, choices=(25, 100, 500, 1000, 10000))
    args = parser.parse_args()
    if args.mode == "compare":
        compare()
    else:
        atomic(args.count, args.implementation)
