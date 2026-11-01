"""Matched native financial execution with driver-level timing on both implementations."""

import argparse
import cProfile
import hashlib
import json
import pstats
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r7_evidence"
parser = argparse.ArgumentParser()
parser.add_argument("--implementation", choices=("baseline", "modified"), required=True)
parser.add_argument("--count", type=int, choices=(100, 500, 1000, 2500, 5000, 10000), required=True)
parser.add_argument("--tag", required=True)
parser.add_argument("--profile", action="store_true")
args = parser.parse_args()
if args.implementation == "baseline":
    sys.path.insert(0, str(ROOT / ".work/r7/baseline"))

import psycopg  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

import railstorm.store as module  # noqa: E402
from experiments.p2a_campaign import _peak_working_set  # noqa: E402
from experiments.r4_profile import resources  # noqa: E402
from railstorm.audit import reconcile, replay_check, snapshot, state_digest  # noqa: E402
from railstorm.domain import digest  # noqa: E402
from railstorm.p2a_oracle import independent_audit  # noqa: E402
from railstorm.scenarios import Scenario  # noqa: E402
from railstorm.simulation import create_run, metrics, step  # noqa: E402
from railstorm.store import Store  # noqa: E402
from scripts.r7_preflight import cpu_ticks  # noqa: E402


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
        check=True,
        timeout=10,
    )
    value = json.loads(result.stdout)
    return value if isinstance(value, list) else [value]


def db_sample(store):
    with store.engine.connect() as conn:
        stats = dict(
            conn.execute(
                text(
                    "SELECT xact_commit,xact_rollback,blks_read,blks_hit,tup_inserted,tup_updated,deadlocks,temp_bytes FROM pg_stat_database WHERE datname=current_database()"
                )
            )
            .mappings()
            .one()
        )
        stats["wal_lsn"] = conn.exec_driver_sql("SELECT pg_current_wal_insert_lsn()::text").scalar_one()
        stats["database_bytes"] = conn.exec_driver_sql(
            "SELECT pg_database_size(current_database())"
        ).scalar_one()
        stats["shared_preload_libraries"] = conn.exec_driver_sql("SHOW shared_preload_libraries").scalar_one()
    return stats


class Instrument:
    def __init__(self):
        self.sql: dict[str, dict] = {}
        self.driver: dict[str, Any] = {"wait_calls": 0, "wait_seconds": 0.0, "commits": [], "rollbacks": []}
        self.python = {
            "event_hash_calls": 0,
            "event_hash_seconds": 0.0,
            "serialization_calls": 0,
            "serialization_seconds": 0.0,
        }
        self.originals = []

    def install(self):
        for name in ("execute", "executemany"):
            original = getattr(psycopg.Cursor, name)

            def wrapped(cursor, query, params=None, *extra, _name=name, _original=original, **kwargs):
                statement = query.decode() if isinstance(query, bytes) else str(query)
                key = " ".join(statement.split())
                row = self.sql.setdefault(key, {"calls": 0, "logical": 0, "seconds": 0.0, "errors": 0})
                row["calls"] += 1
                row["logical"] += len(params) if _name == "executemany" and hasattr(params, "__len__") else 1
                started = time.perf_counter()
                try:
                    return _original(cursor, query, params, *extra, **kwargs)
                except BaseException:
                    row["errors"] += 1
                    raise
                finally:
                    row["seconds"] += time.perf_counter() - started

            self.originals.append((psycopg.Cursor, name, original))
            setattr(psycopg.Cursor, name, wrapped)
        for name in ("wait", "commit", "rollback"):
            original = getattr(psycopg.Connection, name)

            def connection_call(connection, *params, _name=name, _original=original, **kwargs):
                started = time.perf_counter()
                try:
                    return _original(connection, *params, **kwargs)
                finally:
                    elapsed = time.perf_counter() - started
                    if _name == "wait":
                        self.driver["wait_calls"] += 1
                        self.driver["wait_seconds"] += elapsed
                    else:
                        self.driver[_name + "s"].append(elapsed)

            self.originals.append((psycopg.Connection, name, original))
            setattr(psycopg.Connection, name, connection_call)
        original_digest = module.digest
        original_canonical = module.canonical

        def hash_value(value):
            started = time.perf_counter()
            result = original_digest(value)
            if isinstance(value, dict) and "event_type" in value:
                self.python["event_hash_calls"] += 1
                self.python["event_hash_seconds"] += time.perf_counter() - started
            return result

        def encode(value):
            started = time.perf_counter()
            result = original_canonical(value)
            self.python["serialization_calls"] += 1
            self.python["serialization_seconds"] += time.perf_counter() - started
            return result

        self.originals += [(module, "digest", original_digest), (module, "canonical", original_canonical)]
        module.digest = hash_value
        module.canonical = encode

    def restore(self):
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)


def main():
    path = OUT / (args.tag + ".json")
    assert not path.exists(), "Never overwrite a measurement"
    if args.count > 1000:
        previous_count = {2500: 1000, 5000: 2500, 10000: 5000}[args.count]
        previous = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in OUT.glob(f"stable1-{previous_count}-*-modified.json")
        ]
        justified = (
            len(previous) >= 2
            and min(r["payments_per_second"] for r in previous) >= 180
            and all(
                r["audits"]["independent"]["result"]["pass"] and r["audits"]["replay"]["result"]["match"]
                for r in previous
            )
        )
        if not justified:
            (OUT / (args.tag + "-performance-stop.json")).write_text(
                json.dumps(
                    {
                        "status": "NOT_STARTED",
                        "required_prior_tier": previous_count,
                        "required_sustained_per_second": 180,
                        "reason": "No matched prior-tier headroom; larger run unjustified",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            raise RuntimeError("Larger-tier performance gate: no credible180/s headroom")
    available = resources()
    required = (768 if args.count == 100 else 2048) * 1024**2
    if available["ram_available"] < required:
        (OUT / (args.tag + "-resource-stop.json")).write_text(
            json.dumps({"status": "NOT_STARTED", "resources": available, "required_ram": required}, indent=2)
        )
        raise RuntimeError("Host headroom below frozen R7 gate")
    assert available["disk_free"] > 3 * 1024**3
    store = Store()
    settings = {}
    with store.engine.connect() as conn:
        for key in ("server_version", "fsync", "synchronous_commit", "shared_buffers", "max_connections"):
            settings[key] = conn.exec_driver_sql("SHOW " + key).scalar_one()
    assert settings["fsync"] == settings["synchronous_commit"] == "on"
    workload = Scenario.model_validate_json(
        (ROOT / "scenarios/stress_10000.json").read_text(encoding="utf-8")
    ).model_copy(update={"payment_count": args.count})
    run = "r7-" + uuid.uuid4().hex
    (OUT / (args.tag + "-attempt.json")).write_text(
        json.dumps({"status": "STARTED", "run_id": run, "resources": available}, indent=2)
    )
    db_before, server_before = db_sample(store), server_sample()
    observer = Instrument()
    connections = {"checkouts": 0, "max_checked_out": 0, "connection_acquisition_seconds": 0.0}
    locks = []
    lock_start = {}

    def after(conn, cursor, statement, parameters, context, many):
        if "FROM runs WHERE id=" in statement and "FOR UPDATE" in statement:
            lock_start[id(conn)] = time.perf_counter()

    def end(conn):
        if id(conn) in lock_start:
            locks.append(time.perf_counter() - lock_start.pop(id(conn)))

    def checkout(dbapi, record, proxy):
        connections["checkouts"] += 1
        connections["max_checked_out"] = max(
            connections["max_checked_out"], getattr(store.engine.pool, "checkedout")()
        )

    setattr(after, "railstorm_pipeline_safe", True)
    original_acquire = store.engine.pool.connect

    def acquire():
        started = time.perf_counter()
        result = original_acquire()
        connections["connection_acquisition_seconds"] += time.perf_counter() - started
        return result

    setattr(store.engine.pool, "connect", acquire)

    event.listen(store.engine, "after_cursor_execute", after)
    event.listen(store.engine, "commit", end)
    event.listen(store.engine.pool, "checkout", checkout)
    profiler = cProfile.Profile() if args.profile else None
    observer.install()
    host_before = cpu_ticks()
    started, cpu = time.perf_counter(), time.process_time()
    try:
        if profiler:
            profiler.enable()
        create_run(store, run, workload)
        while not step(store, run, 1000)["done"]:
            if resources()["ram_available"] < 512 * 1024**2:
                raise RuntimeError("Low memory after committed batch; incomplete world retained")
        elapsed, cpu = time.perf_counter() - started, time.process_time() - cpu
        host_after = cpu_ticks()
        if profiler:
            profiler.disable()
    except BaseException as error:
        (OUT / (args.tag + "-incomplete.json")).write_text(
            json.dumps(
                {
                    "run_id": run,
                    "error": type(error).__name__,
                    "elapsed": time.perf_counter() - started,
                    "resources": resources(),
                },
                indent=2,
            )
        )
        raise
    finally:
        observer.restore()
        setattr(store.engine.pool, "connect", original_acquire)
        event.remove(store.engine, "after_cursor_execute", after)
        event.remove(store.engine, "commit", end)
        event.remove(store.engine.pool, "checkout", checkout)
    server_after, db_after = server_sample(), db_sample(store)
    audits = {}
    for name, function in (
        ("native", reconcile),
        ("replay", replay_check),
        ("independent", independent_audit),
    ):
        started = time.perf_counter()
        audits[name] = {"result": function(store, run), "seconds": time.perf_counter() - started}
    assert (
        audits["native"]["result"]["financial_errors"] == 0
        and audits["replay"]["result"]["match"]
        and audits["independent"]["result"]["pass"]
    )
    measured = metrics(store, run)
    assert measured["settled"] == args.count and measured["pending"] == measured["missing_ack"] == 0
    with store.transaction(run) as tx:
        state = snapshot(tx)
        counts = {
            name: tx.one("SELECT count(*) n FROM " + name + " WHERE run_id=:r")["n"]
            for name in (
                "payments",
                "quotes",
                "decisions",
                "reservations",
                "journal_entries",
                "journal_lines",
                "events",
                "outbox",
            )
        }
    functions = []
    if profiler:
        for (file, line, name), (_, calls, own, inclusive, _) in getattr(
            pstats.Stats(profiler), "stats"
        ).items():
            functions.append(
                {
                    "file": Path(file).name,
                    "name": name,
                    "calls": calls,
                    "self_seconds": own,
                    "inclusive_seconds": inclusive,
                }
            )
    source_file = module.__file__
    assert source_file
    report = {
        "implementation": args.implementation,
        "count": args.count,
        "run_id": run,
        "execution_seconds": elapsed,
        "payments_per_second": args.count / elapsed,
        "cpu_seconds": cpu,
        "host_cpu_ticks_before": host_before,
        "host_cpu_ticks_after": host_after,
        "peak_client_bytes": _peak_working_set(),
        "workload_hash": digest(workload.model_dump()),
        "workload": workload.model_dump(),
        "settings": settings,
        "resources_before": available,
        "resources_after": resources(),
        "counts": counts,
        "financial_state_hash": state_digest(state),
        "metrics": measured,
        "audits": audits,
        "sql": observer.sql,
        "driver": observer.driver,
        "python": observer.python,
        "pool": connections,
        "run_lock_seconds_excluding_final_commit": locks,
        "postgres_before": server_before,
        "postgres_after": server_after,
        "database_before": db_before,
        "database_after": db_after,
        "profile_self": sorted(functions, key=lambda r: r["self_seconds"], reverse=True)[:45],
        "profile_inclusive": sorted(functions, key=lambda r: r["inclusive_seconds"], reverse=True)[:30],
        "source_sha256": {
            p.relative_to(Path(source_file).parent).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(source_file).parent.rglob("*.py")
        },
        "scope": "Same raw driver instrumentation both sides; wait_calls are protocol progress invocations, not packet RTTs. Commit latency actual driver call. Server stats asynchronous/global WAL; no exclusive server attribution. CPU/RSS client; server process samples separate. cProfile runs excluded from matched medians.",
    }
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "implementation",
                    "count",
                    "execution_seconds",
                    "payments_per_second",
                    "cpu_seconds",
                )
            }
        ),
        flush=True,
    )
    store.engine.dispose()


if __name__ == "__main__":
    main()
