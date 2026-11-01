"""Frozen R5 versus R6 native atomic contract; instrumentation never changes commands."""

import argparse
import cProfile
import hashlib
import json
import pstats
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r6_evidence"
parser = argparse.ArgumentParser()
parser.add_argument("--implementation", choices=("baseline", "modified"), required=True)
parser.add_argument("--count", type=int, choices=(25, 100, 500, 1000, 10000), required=True)
parser.add_argument("--tag", required=True)
parser.add_argument("--profile", action="store_true")
args = parser.parse_args()
if args.implementation == "baseline":
    sys.path.insert(0, str(ROOT / ".work/r6/baseline"))

from sqlalchemy import event  # noqa: E402
from sqlalchemy.pool import QueuePool  # noqa: E402

from experiments.p2a_campaign import _peak_working_set  # noqa: E402
from experiments.r4_profile import resources  # noqa: E402
from railstorm.audit import reconcile, replay_check, snapshot, state_digest  # noqa: E402
from railstorm.domain import digest  # noqa: E402
from railstorm.p2a_oracle import independent_audit  # noqa: E402
from railstorm.scenarios import Scenario  # noqa: E402
from railstorm.simulation import create_run, metrics, step  # noqa: E402
from railstorm.store import Store  # noqa: E402


def server_sample():
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process postgres | Select-Object Id,CPU,WorkingSet64,PeakWorkingSet64 | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    rows = json.loads(result.stdout)
    return rows if isinstance(rows, list) else [rows]


def main():
    path = OUT / (args.tag + ".json")
    if path.exists():
        raise FileExistsError(path)
    before_resources = resources()
    required_ram = (512 if args.count <= 100 else 768 if args.count <= 1000 else 1536) * 1024**2
    if before_resources["ram_available"] <= required_ram:
        (OUT / (args.tag + "-resource-stop.json")).write_text(
            json.dumps(
                {"resources": before_resources, "required_free_ram": required_ram, "status": "NOT_STARTED"},
                indent=2,
            )
        )
    assert before_resources["ram_available"] > required_ram, "Resource gate: insufficient free RAM"
    assert before_resources["disk_free"] > 2 * 1024**3
    store = Store()
    assert isinstance(store.engine.pool, QueuePool)
    pool = store.engine.pool
    store_file = sys.modules["railstorm.store"].__file__
    assert store_file is not None
    source_path = Path(store_file)
    run = "r6-" + uuid.uuid4().hex
    spec = Scenario.model_validate_json((ROOT / "scenarios/stress_10000.json").read_text()).model_copy(
        update={"payment_count": args.count}
    )
    config = {}
    with store.engine.connect() as conn:
        for key in ("server_version", "fsync", "synchronous_commit", "shared_buffers", "max_connections"):
            config[key] = conn.exec_driver_sql("SHOW " + key).scalar_one()
        assert config["fsync"] == config["synchronous_commit"] == "on"
    sql: dict[str, dict] = {}
    transactions = {"begins": 0, "commits": 0, "rollbacks": 0, "checkouts": 0, "max_checked_out": 0}
    locks: list[float] = []
    starts = {}

    def before(conn, cursor, statement, parameters, context, many):
        context.r6_start = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, many):
        key = " ".join(statement.split())
        row = sql.setdefault(key, {"calls": 0, "seconds": 0.0, "logical": 0})
        row["calls"] += 1
        row["logical"] += len(parameters) if many else 1
        row["seconds"] += time.perf_counter() - context.r6_start
        if "FROM runs WHERE id=" in statement and "FOR UPDATE" in statement:
            starts[id(conn)] = time.perf_counter()

    def begin(conn):
        transactions["begins"] += 1

    def finish(conn, kind):
        transactions[kind] += 1
        start = starts.pop(id(conn), None)
        if start is not None:
            locks.append(time.perf_counter() - start)

    def checkout(dbapi, record, proxy):
        transactions["checkouts"] += 1
        transactions["max_checked_out"] = max(transactions["max_checked_out"], pool.checkedout())

    listeners: list[tuple[str, Callable[..., Any]]] = [
        ("before_cursor_execute", before),
        ("after_cursor_execute", after),
        ("begin", begin),
        ("commit", lambda c: finish(c, "commits")),
        ("rollback", lambda c: finish(c, "rollbacks")),
    ]
    for name, callback in listeners:
        event.listen(store.engine, name, callback)
    event.listen(store.engine.pool, "checkout", checkout)
    profiler = cProfile.Profile() if args.profile else None
    (OUT / (args.tag + "-attempt.json")).write_text(json.dumps({"run": run, "status": "STARTED"}))
    server_before = server_sample()
    cpu, started = time.process_time(), time.perf_counter()
    if profiler:
        profiler.enable()
    create_run(store, run, spec)
    while not step(store, run, 1000)["done"]:
        if resources()["ram_available"] < 256 * 1024**2:
            (OUT / (args.tag + "-incomplete.json")).write_text(
                json.dumps(
                    {"run": run, "status": "RESOURCE_STOP_AFTER_COMMITTED_BATCH", "resources": resources()},
                    indent=2,
                )
            )
            raise RuntimeError("Stopped at committed queue boundary: low free RAM")
    if profiler:
        profiler.disable()
    elapsed = time.perf_counter() - started
    cpu = time.process_time() - cpu
    server_after = server_sample()
    for name, callback in listeners:
        event.remove(store.engine, name, callback)
    event.remove(store.engine.pool, "checkout", checkout)
    peak = _peak_working_set()
    audits = {}
    for name, function in (
        ("reconciliation", reconcile),
        ("replay", replay_check),
        ("independent", independent_audit),
    ):
        started = time.perf_counter()
        audits[name] = {"result": function(store, run), "seconds": time.perf_counter() - started}
    assert audits["reconciliation"]["result"]["financial_errors"] == 0
    assert audits["replay"]["result"]["match"] and audits["independent"]["result"]["pass"]
    measured = metrics(store, run)
    assert measured["settled"] == args.count and measured["pending"] == 0
    with store.transaction(run) as tx:
        state = snapshot(tx)
        counts = {
            table: tx.one("SELECT count(*) n FROM " + table + " WHERE run_id=:r")["n"]
            for table in ("payments", "journal_entries", "journal_lines", "events", "outbox", "reservations")
        }
    functions = []
    if profiler:
        for (file, line, name), (primitive, calls, own, inclusive, callers) in getattr(
            pstats.Stats(profiler), "stats"
        ).items():
            functions.append(
                {"file": Path(file).name, "name": name, "calls": calls, "self": own, "inclusive": inclusive}
            )
    report = {
        "implementation": args.implementation,
        "count": args.count,
        "run_id": run,
        "execution_seconds": elapsed,
        "payments_per_second": args.count / elapsed,
        "cpu_seconds": cpu,
        "cpu_one_core_percent": 100 * cpu / elapsed,
        "peak_client_bytes": peak,
        "resources_before": before_resources,
        "resources_after": resources(),
        "database": config,
        "workload": spec.model_dump(),
        "workload_hash": digest(spec.model_dump()),
        "postgres_processes_before": server_before,
        "postgres_processes_after": server_after,
        "state_hash": state_digest(state),
        "metrics": measured,
        "counts": counts,
        "audits": audits,
        "sql": sql,
        "cursor_calls": sum(x["calls"] for x in sql.values()),
        "cursor_seconds": sum(x["seconds"] for x in sql.values()),
        "transactions": transactions,
        "run_lock_seconds": locks,
        "source_store_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "runtime_source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_path.parent.glob("*.py")
        },
        "profile": sorted(functions, key=lambda x: x["self"], reverse=True)[:45],
        "measurement_scope": "client wall includes create_run and committed step1000; CPU/RSS client only; SQL cursor calls not network packet counts; lock durations exclude final commit latency",
    }
    path.write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "implementation",
                    "count",
                    "execution_seconds",
                    "payments_per_second",
                    "cursor_calls",
                    "cursor_seconds",
                )
            }
        ),
        flush=True,
    )
    store.engine.dispose()


if __name__ == "__main__":
    main()
