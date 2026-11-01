"""One small final-source diagnostic for SQL phases, commits and Python calls."""

import cProfile
import json
import pstats
import time
import uuid

from sqlalchemy import event

from experiments.r4_benchmark import OUT, ROOT
from railstorm.audit import reconcile, replay_check
from railstorm.scenarios import Scenario
from railstorm.simulation import create_run, step
from railstorm.store import Store


def main():
    store = Store()
    phase = "execution"
    sql: dict[str, dict] = {}
    transactions = {"begin": 0, "commit": 0, "rollback": 0}

    def before(conn, cursor, statement, parameters, context, executemany):
        context.r4_component_start = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, executemany):
        upper = statement.upper().strip()
        if "MARKET_QUOTE" in upper or "FROM QUOTES" in upper:
            kind = "quote_reads"
        elif upper.startswith("SELECT") and "FROM POSITIONS" in upper:
            kind = "liquidity_reads"
        elif upper.startswith("UPDATE POSITIONS"):
            kind = "projection_updates"
        elif upper.startswith("INSERT INTO JOURNAL"):
            kind = "journal_inserts"
        elif upper.startswith(("INSERT INTO EVENTS", "INSERT INTO OUTBOX")):
            kind = "event_persistence"
        elif "FROM EVENTS" in upper:
            kind = "history_reads"
        elif "FOR UPDATE" in upper:
            kind = "run_lock_reads"
        else:
            kind = "other"
        key = phase + ":" + kind
        row = sql.setdefault(key, {"cursor_calls": 0, "logical_executions": 0, "wall_seconds": 0.0})
        row["cursor_calls"] += 1
        row["logical_executions"] += len(parameters) if executemany else 1
        row["wall_seconds"] += time.perf_counter() - context.r4_component_start

    event.listen(store.engine, "before_cursor_execute", before)
    event.listen(store.engine, "after_cursor_execute", after)

    def transaction_event(name):
        def count(conn):
            transactions[name] += 1

        return count

    for name in transactions:
        event.listen(store.engine, name, transaction_event(name))
    run = "r4-components-" + uuid.uuid4().hex
    spec = Scenario.model_validate_json((ROOT / "scenarios/stress_10000.json").read_text()).model_copy(
        update={"payment_count": 25}
    )
    profiler = cProfile.Profile()
    profiler.enable()
    started = time.perf_counter()
    create_run(store, run, spec)
    while not step(store, run, 1000)["done"]:
        pass
    execution = time.perf_counter() - started
    phase = "audit"
    started = time.perf_counter()
    audit = reconcile(store, run)
    audit_seconds = time.perf_counter() - started
    phase = "replay"
    started = time.perf_counter()
    replay = replay_check(store, run)
    replay_seconds = time.perf_counter() - started
    profiler.disable()
    assert audit["financial_errors"] == 0 and replay["match"]
    functions = []
    from pathlib import Path

    for (file, line, name), (primitive, calls, self_s, inclusive_s, callers) in getattr(
        pstats.Stats(profiler), "stats"
    ).items():
        functions.append(
            {
                "file": Path(file).name,
                "line": line,
                "function": name,
                "calls": calls,
                "self_seconds": self_s,
                "inclusive_seconds": inclusive_s,
            }
        )
    result = {
        "scope": "25 atomic payments; cProfile diagnostic, not matched throughput result; inclusive timings overlap",
        "execution_seconds": execution,
        "audit_seconds": audit_seconds,
        "replay_seconds": replay_seconds,
        "sql_phases": sql,
        "transaction_events": transactions,
        "functions": functions,
        "audit": audit,
        "replay": replay,
    }
    started = time.perf_counter()
    serialized = json.dumps(result, indent=2)
    serialization = time.perf_counter() - started
    result["evidence_json_serialization_seconds"] = serialization
    result["evidence_json_bytes"] = len(serialized.encode())
    (OUT / "component_profile.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                "execution": execution,
                "audit": audit_seconds,
                "replay": replay_seconds,
                "serialization": serialization,
                "transactions": transactions,
            }
        )
    )


if __name__ == "__main__":
    main()
