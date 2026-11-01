"""Genuine subprocess death at an instant-link recovery commit boundary."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.instant_link import InstantLink
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]


def _state(store, run):
    rail = InstantLink(store)
    with store.engine.connect() as conn:
        p = {"r": run, "p": "payment"}
        journal = [
            dict(x)
            for x in conn.execute(
                text("SELECT id,currency FROM journal_entries WHERE run_id=:r AND payment_id=:p ORDER BY id"),
                p,
            ).mappings()
        ]
        events = [
            dict(x)
            for x in conn.execute(
                text(
                    "SELECT seq,event_type FROM events WHERE run_id=:r AND event_type='IL_RECOVERY' ORDER BY seq"
                ),
                p,
            ).mappings()
        ]
        beneficiary = conn.execute(
            text("SELECT posted FROM positions WHERE run_id=:r AND account_id='MERIDIAN_SG:SGD'"), p
        ).scalar_one()
    return {
        "payment": rail.inspect(run, "payment")["payment"],
        "messages": rail.messages(run, "payment"),
        "journals": journal,
        "recovery_events": events,
        "beneficiary_posted_minor": beneficiary,
        "independent": independent_audit(store, run),
        "native": reconcile(store, run),
        "replay": replay_check(store, run),
    }


def exercise(before_posting=False, artifact_name="P2_A_R_crash.json"):
    admin = Store()
    database = "railstorm_p2ar_crash_" + uuid.uuid4().hex[:8]
    assert database.startswith("railstorm_p2ar_crash_")
    with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = admin.engine.url.set(database=database).render_as_string(hide_password=False)
    env = {**os.environ, "RAILSTORM_DATABASE_URL": url}
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    store = Store(url)
    rail = InstantLink(store)
    run = "p2ar-crash-" + uuid.uuid4().hex
    rail.initialize(run)
    rail.prepare(
        run, PaymentCommand(idempotency_key="payment", principal_minor=10000, requested_rail="INSTANT_LINK")
    )
    rail.deliver(run, "payment", "SOURCE", 10)
    rail.deliver(run, "payment", "DESTINATION", 30, available=False)
    before = _state(store, run)
    assert before["payment"]["source_final"] and not before["payment"]["destination_final"]
    assert before["payment"]["obligation"]["amount_minor"] == 13300
    assert before["independent"]["pass"] and before["native"]["financial_errors"] == 0
    worker = subprocess.run(
        [
            sys.executable,
            "-m",
            "experiments.r2_crash_worker" if before_posting else "experiments.p2ar_crash_worker",
            run,
            "payment",
        ],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert worker.returncode == (74 if before_posting else 73), (worker.returncode, worker.stderr[-500:])
    interrupted = _state(store, run)
    if before_posting:
        assert not interrupted["payment"]["destination_final"]
        assert interrupted["payment"]["obligation"]["amount_minor"] == 13300
        assert interrupted["beneficiary_posted_minor"] == 0 and len(interrupted["journals"]) == 1
    else:
        assert interrupted["payment"]["destination_final"]
        assert interrupted["payment"]["economic_status"] == "SETTLED"
        assert interrupted["payment"]["obligation"] is None
        assert interrupted["beneficiary_posted_minor"] == 13300
        assert len(interrupted["journals"]) == 2 and not interrupted["recovery_events"]
        assert any(m["type"] == "DESTINATION" and m["ack_status"] == "LOST" for m in interrupted["messages"])
    assert interrupted["independent"]["pass"] and interrupted["native"]["financial_errors"] == 0
    assert interrupted["replay"]["match"]
    restarted = InstantLink(Store(url))
    try:
        first_recovery = restarted.recover(run, "payment", 41)
        second_recovery = restarted.recover(run, "payment", 42)
    finally:
        restarted.store.engine.dispose()
    after = _state(store, run)
    assert after["payment"]["economic_status"] == "SETTLED"
    assert len(after["journals"]) == 2 and after["beneficiary_posted_minor"] == 13300
    assert len(after["recovery_events"]) == 2
    assert all(m["ack_status"] == "ACKNOWLEDGED" for m in after["messages"])
    assert after["independent"]["pass"] and after["native"]["financial_errors"] == 0
    assert after["replay"]["match"]
    result = {
        "database": database,
        "database_disposable": True,
        "migration_head": "007",
        "migration_output": migration.stdout + migration.stderr,
        "worker_exit_code": worker.returncode,
        "worker_phase": "before destination recovery posting"
        if before_posting
        else "destination financial/message commit durable; recovery lifecycle event absent",
        "run_id": run,
        "before": before,
        "after_process_exit": interrupted,
        "first_restart_recovery": first_recovery,
        "second_restart_recovery": second_recovery,
        "after_recovery": after,
    }
    (ROOT / "docs" / artifact_name).write_text(json.dumps(result, indent=2), encoding="utf-8")
    store.engine.dispose()
    with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
    return result


if __name__ == "__main__":
    result = exercise()
    print("Process crash and restart passed", result["worker_exit_code"])
