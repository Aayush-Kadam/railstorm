"""Two independent PostgreSQL sessions contend for one bank-local funded claim."""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import text

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand
from railstorm.ledger import Ledger
from railstorm.p2a_oracle import independent_audit
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]


def exercise(store: Store):
    run = "p2ar-contention-" + uuid.uuid4().hex
    Ledger(store).initialize(
        run, {"fee_minor": 25, "funding": {"ATLAS_US:USD": 15000, "STERLING_UK:SGD": 50000}}
    )
    with store.transaction(run) as tx:
        initial = tx.position("ATLAS_US:USD")
    assert initial["posted"] == 15000 and initial["reserved"] == 0 and initial["buffer"] == 0
    required_each = 10000 + 25
    assert required_each <= 15000 < 2 * required_each
    observed_waits = []

    def worker(identity):
        independent = Store()
        try:
            return Ledger(independent).prepare(
                run, PaymentCommand(idempotency_key=identity, principal_minor=10000)
            )
        finally:
            independent.engine.dispose()

    def blocked_count():
        with store.engine.connect() as connection:
            return connection.execute(
                text("""SELECT count(*) FROM pg_stat_activity
                WHERE datname=current_database() AND wait_event_type='Lock'
                AND query LIKE '%FROM runs%'""")
            ).scalar_one()

    def await_blocked(target):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            count = blocked_count()
            observed_waits.append(count)
            if count >= target:
                return
            time.sleep(0.02)
        raise AssertionError(f"Only {max(observed_waits, default=0)} sessions observed waiting")

    with store.engine.connect() as gate:
        transaction = gate.begin()
        gate.execute(text("SELECT id FROM runs WHERE id=:r FOR UPDATE"), {"r": run})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker, "first")
            await_blocked(1)
            second = pool.submit(worker, "second")
            await_blocked(2)
            transaction.commit()
            outcomes = {"first": first.result(timeout=20), "second": second.result(timeout=20)}
    assert sorted(x["economic_status"] for x in outcomes.values()) == ["PREPARED", "REJECTED"]
    winner = next(identity for identity, state in outcomes.items() if state["economic_status"] == "PREPARED")
    loser = next(identity for identity, state in outcomes.items() if state["economic_status"] == "REJECTED")
    assert outcomes[loser]["reason"] == "INSUFFICIENT_LIQUIDITY"
    Ledger(store).settle(run, winner, 10)
    Ledger(store).acknowledge(run, winner)
    with store.transaction(run) as tx:
        final = tx.position("ATLAS_US:USD")
        decisions = tx.rows("SELECT payment_id,decision FROM decisions WHERE run_id=:r ORDER BY payment_id")
        reservations = tx.rows("SELECT * FROM reservations WHERE run_id=:r ORDER BY id")
        postings = tx.rows(
            "SELECT id,payment_id,currency FROM journal_entries WHERE run_id=:r AND payment_id IS NOT NULL ORDER BY id"
        )
        payments = tx.rows("SELECT id,data FROM payments WHERE run_id=:r ORDER BY id")
        obligations = tx.rows("SELECT * FROM cb_obligations WHERE run_id=:r")
    assert final["posted"] == 4975 and final["reserved"] == 0
    assert decisions == [{"payment_id": winner, "decision": "COMMIT"}]
    assert len(postings) == 2 and all(x["payment_id"] == winner for x in postings)
    assert len(reservations) == 2 and all(x["status"] == "CONSUMED" for x in reservations)
    native = reconcile(store, run)
    replay = replay_check(store, run)
    independent = independent_audit(store, run)
    assert native["financial_errors"] == 0 and replay["match"] and independent["pass"]
    return {
        "run_id": run,
        "initial_source": {k: initial[k] for k in ("posted", "reserved", "buffer")},
        "required_each_minor": required_each,
        "combined_required_minor": 2 * required_each,
        "observed_lock_waits": observed_waits,
        "winner": winner,
        "loser": loser,
        "prepare_outcomes": outcomes,
        "final_source": {k: final[k] for k in ("posted", "reserved", "buffer")},
        "payments": payments,
        "decisions": decisions,
        "reservations": reservations,
        "postings": postings,
        "obligations": obligations,
        "native_reconciliation": native,
        "replay": replay,
        "independent_audit": independent,
    }


if __name__ == "__main__":
    result = exercise(Store())
    path = ROOT / "docs/P2_A_R_contention.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Contention passed", result["winner"], result["loser"], max(result["observed_lock_waits"]))
