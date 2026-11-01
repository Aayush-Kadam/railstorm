"""Non-semantic benchmark instrumentation. Source snapshot can be selected explicitly."""

import argparse
import cProfile
import ctypes
import hashlib
import importlib.metadata
import json
import os
import platform
import pstats
import sys
import time
from collections import defaultdict
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path)
parser.add_argument("--counts", nargs="+", type=int, default=[100, 1000, 5000, 10000])
parser.add_argument("--label", required=True)
parser.add_argument("--profile", action="store_true")
args = parser.parse_args()
if args.source:
    sys.path.insert(0, str(args.source.resolve()))

from sqlalchemy import event, text  # noqa: E402

from railstorm.audit import reconcile, replay_check  # noqa: E402
from railstorm.scenarios import Scenario  # noqa: E402
from railstorm.simulation import create_run, metrics, step  # noqa: E402
from railstorm.store import Store  # noqa: E402


def memory():
    if os.name != "nt":
        import resource

        return {"client_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}

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
            )
        ]

    value = Counters()
    value.cb = ctypes.sizeof(value)
    from ctypes import wintypes

    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.restype = wintypes.HANDLE
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    get_memory.restype = wintypes.BOOL
    handle = get_process()
    if not get_memory(handle, ctypes.byref(value), value.cb):
        raise RuntimeError("Memory measurement failed")
    return {
        "client_peak_rss_bytes": value.PeakWorkingSetSize,
        "client_current_rss_bytes": value.WorkingSetSize,
    }


def system_resources():
    if os.name != "nt":
        return {}
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong)
            for name in (
                "total_phys",
                "available_phys",
                "total_page",
                "available_page",
                "total_virtual",
                "available_virtual",
                "extended",
            )
        ]

    value = MemoryStatus()
    value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise RuntimeError("System RAM measurement failed")
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        raise RuntimeError("System CPU measurement failed")

    def ticks(t):
        return (t.dwHighDateTime << 32) + t.dwLowDateTime

    return {
        "physical_ram_bytes": value.total_phys,
        "available_physical_ram_bytes": value.available_phys,
        "cpu_total_ticks": ticks(kernel) + ticks(user),
        "cpu_idle_ticks": ticks(idle),
    }


class Instrumentation:
    def __init__(self, engine):
        self.phase = "setup"
        self.data = defaultdict(
            lambda: {
                "sql_count": 0,
                "logical_sql_statement_count": 0,
                "sql_seconds": 0.0,
                "commit_count": 0,
                "rollback_count": 0,
                "transaction_seconds": 0.0,
                "max_transaction_seconds": 0.0,
                "sql": {},
            }
        )
        self.starts = {}
        self.errors = defaultdict(int)

        @event.listens_for(engine, "before_cursor_execute")
        def before(conn, cursor, statement, parameters, context, executemany):
            context._p01_start = time.perf_counter()

        @event.listens_for(engine, "after_cursor_execute")
        def after(conn, cursor, statement, parameters, context, executemany):
            elapsed = time.perf_counter() - context._p01_start
            data = self.data[self.phase]
            data["sql_count"] += 1
            data["logical_sql_statement_count"] += len(parameters) if executemany else 1
            data["sql_seconds"] += elapsed
            key = " ".join(statement.split())
            q = data["sql"].setdefault(key, {"calls": 0, "seconds": 0.0, "executemany_calls": 0})
            q["calls"] += 1
            q["seconds"] += elapsed
            q["executemany_calls"] += int(executemany)

        @event.listens_for(engine, "begin")
        def begin(conn):
            self.starts[id(conn.connection.driver_connection)] = time.perf_counter()

        original_commit = engine.dialect.do_commit
        original_rollback = engine.dialect.do_rollback

        def finish(connection, operation, committed):
            driver = getattr(connection, "driver_connection", connection)
            started = self.starts.pop(id(driver), None)
            result = operation(connection)
            if started is not None:
                elapsed = time.perf_counter() - started
                data = self.data[self.phase]
                data["commit_count" if committed else "rollback_count"] += 1
                data["transaction_seconds"] += elapsed
                data["max_transaction_seconds"] = max(data["max_transaction_seconds"], elapsed)
            return result

        engine.dialect.do_commit = lambda c: finish(c, original_commit, True)
        engine.dialect.do_rollback = lambda c: finish(c, original_rollback, False)

        @event.listens_for(engine, "handle_error")
        def error(context):
            self.errors[str(getattr(context.original_exception, "sqlstate", "UNKNOWN"))] += 1

    def result(self):
        result = dict(self.data)
        for phase in result.values():
            n = phase["commit_count"] + phase["rollback_count"]
            phase["mean_transaction_seconds"] = phase["transaction_seconds"] / n if n else None
            phase["sql"] = dict(sorted(phase["sql"].items(), key=lambda p: p[1]["seconds"], reverse=True))
        return result


store = Store()
out = Path("experiments/reports/p01")
out.mkdir(parents=True, exist_ok=True)
with store.engine.connect() as conn:
    server = dict(
        conn.execute(
            text(
                "SELECT version() version,current_setting('fsync') fsync,current_setting('synchronous_commit') synchronous_commit,current_setting('shared_buffers') shared_buffers"
            )
        )
        .mappings()
        .one()
    )
    old = conn.execute(text("SELECT count(*) FROM runs")).scalar_one()
try:
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
        cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0]
except (ImportError, OSError):
    cpu = platform.processor()
source = Path(sys.modules["railstorm.store"].__file__).parent
source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.glob("*.py"))}
for count in args.counts:
    scenario_config = json.loads(Path("scenarios/stress_10000.json").read_text())
    scenario_config["payment_count"] = count
    scenario = Scenario.model_validate(scenario_config)
    run_id = f"p01-{args.label}-{count}"
    instrumentation = Instrumentation(store.engine)
    # A new engine for each run keeps listeners isolated and SQL compilation state comparable.
    profiler = cProfile.Profile() if args.profile else None
    instrumentation.phase = "execution"
    machine_before = system_resources()
    start_cpu, start = time.process_time(), time.perf_counter()
    if profiler:
        profiler.enable()
    create_run(store, run_id, scenario)
    while not step(store, run_id, 1000)["done"]:
        pass
    if profiler:
        profiler.disable()
    execution = time.perf_counter() - start
    cpu_seconds = time.process_time() - start_cpu
    machine_after = system_resources()
    system_cpu_percent = None
    if machine_after:
        total = machine_after["cpu_total_ticks"] - machine_before["cpu_total_ticks"]
        idle = machine_after["cpu_idle_ticks"] - machine_before["cpu_idle_ticks"]
        system_cpu_percent = 100 * (total - idle) / total
    execution_memory = memory()
    instrumentation.phase = "standalone_replay"
    start = time.perf_counter()
    replay = replay_check(store, run_id)
    replay_seconds = time.perf_counter() - start
    instrumentation.phase = "reconciliation_including_replay"
    start = time.perf_counter()
    reconciliation = reconcile(store, run_id)
    reconciliation_seconds = time.perf_counter() - start
    instrumentation.phase = "verification_and_metrics"
    with store.transaction(run_id) as tx:
        counts = {}
        for table in (
            "payments",
            "events",
            "journal_entries",
            "journal_lines",
            "reservations",
            "outbox",
            "decisions",
        ):
            counts[table] = tx.one(f"SELECT count(*) n FROM {table} WHERE run_id=:r")["n"]
        independent = tx.rows(
            """SELECT a.currency,sum(p.posted) total FROM accounts a JOIN positions p ON a.run_id=p.run_id AND a.id=p.account_id WHERE a.run_id=:r AND a.kind='CLAIM' GROUP BY a.currency ORDER BY a.currency"""
        )
        independent = [{"currency": r["currency"], "total": int(r["total"])} for r in independent]
        settled = tx.one(
            "SELECT count(*) n FROM payments WHERE run_id=:r AND data->>'economic_status'='SETTLED'"
        )["n"]
        duplicate = tx.one(
            "SELECT count(*) n FROM (SELECT payment_id FROM journal_entries WHERE run_id=:r AND payment_id IS NOT NULL GROUP BY payment_id HAVING count(*)<>2) x"
        )["n"]
        unbalanced = tx.one(
            "SELECT count(*) n FROM (SELECT entry_id FROM journal_lines WHERE run_id=:r GROUP BY entry_id HAVING sum(delta)<>0) x"
        )["n"]
    result = {
        "label": args.label,
        "run_id": run_id,
        "scenario": scenario.model_dump(),
        "profile_enabled": args.profile,
        "source_sha256": source_hashes,
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "cpu": cpu,
            "logical_cpu_count": os.cpu_count(),
            "postgresql": server,
            "database_mode": "native localhost",
            "preexisting_runs": old,
        },
        "execution_seconds": execution,
        "physical_ram_bytes": machine_before.get("physical_ram_bytes"),
        "library_versions": {
            name: importlib.metadata.version(name)
            for name in ("SQLAlchemy", "psycopg", "fastapi", "pydantic")
        },
        "system_mean_cpu_percent_all_workloads": system_cpu_percent,
        "client_cpu_seconds": cpu_seconds,
        "client_mean_cpu_one_core_percent": 100 * cpu_seconds / execution,
        "execution_memory": execution_memory,
        "whole_process_memory_after_audits": memory(),
        "payments_per_second": count / execution,
        "events_per_second": counts["events"] / execution,
        "standalone_replay_seconds": replay_seconds,
        "reconciliation_including_replay_seconds": reconciliation_seconds,
        "row_counts": counts,
        "independent_currency_totals_minor": independent,
        "independent_settled_count": settled,
        "independent_invalid_leg_group_count": duplicate,
        "independent_unbalanced_count": unbalanced,
        "replay": replay,
        "reconciliation": reconciliation,
        "metrics": metrics(store, run_id),
        "sql_and_transactions": instrumentation.result(),
        "database_error_sqlstates": dict(instrumentation.errors),
        "retries": 0,
        "retry_definition": "Healthy fixture performs no retryable DB operation; fixed RECOVER/EXPIRY queue events are still dispatched.",
    }
    if profiler:
        stats = pstats.Stats(profiler).strip_dirs()
        result["profile_top_cumulative"] = [
            {
                "function": str(k),
                "primitive_calls": v[0],
                "calls": v[1],
                "self_seconds": v[2],
                "cumulative_seconds": v[3],
            }
            for k, v in sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:70]
        ]
    path = out / f"{args.label}-{count}.json"
    path.write_text(json.dumps(result, indent=2))
    assert settled == count and duplicate == 0 and unbalanced == 0
    assert reconciliation["financial_errors"] == 0 and replay["match"]
    print(
        json.dumps(
            {
                "report": str(path),
                "execution_seconds": execution,
                "settled": settled,
                "sql": result["sql_and_transactions"]["execution"]["sql_count"],
                "peak_rss": execution_memory["client_peak_rss_bytes"],
            }
        ),
        flush=True,
    )
    store.engine.dispose()
    store = Store()
