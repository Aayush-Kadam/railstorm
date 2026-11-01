"""Native command result compatibility and both immediate/deferred failure boundaries."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from railstorm.audit import reconcile, replay_check
from railstorm.domain import DomainError, PaymentCommand
from railstorm.p2a_oracle import independent_audit


def test_native_parameters_results_and_sqlalchemy_escape_hatch(world):
    ledger, run = world
    with ledger.store.transaction(run) as tx:
        row = (
            tx.execute("SELECT CAST(:n AS bigint) AS amount, '100%' AS marker, :n AS repeated", n=7)
            .mappings()
            .first()
        )
        assert row == {"amount": 7, "marker": "100%", "repeated": 7}
        assert tx.execute("SELECT :n", n=11).scalar_one() == 11
        assert tx.execute("SELECT :n WHERE false", n=11).scalar() is None
        changed = tx.execute("UPDATE positions SET buffer=1 WHERE run_id=:r AND account_id='ATLAS_US:USD'")
        assert changed.rowcount == 1
        # SQLAlchemy's metadata must be ready even with native writes pending.
        assert (
            tx.conn.execute(
                text("SELECT buffer FROM positions WHERE run_id=:r AND account_id='ATLAS_US:USD'"), {"r": run}
            ).scalar_one()
            == 1
        )
        assert (
            tx.conn.execute(
                text("UPDATE positions SET buffer=0 WHERE run_id=:r AND account_id='ATLAS_US:USD'"),
                {"r": run},
            ).rowcount
            == 1
        )
    assert independent_audit(ledger.store, run)["pass"]


def test_deferred_pipeline_error_rolls_back_money_events_and_cursor(world):
    ledger, run = world
    store = ledger.store
    before = replay_check(store, run)
    with pytest.raises(DBAPIError):
        with store.transaction(run) as tx:
            ledger.submit(run, PaymentCommand(idempotency_key="p", principal_minor=10000))
            tx.execute("INSERT INTO positions(run_id,account_id) VALUES(:r,'MISSING:USD')")
    assert replay_check(store, run)["state_hash"] == before["state_hash"]
    with store.transaction(run) as tx:
        assert tx.payment("p") is None
    assert reconcile(store, run)["financial_errors"] == 0 and independent_audit(store, run)["pass"]


def test_caught_driver_error_cannot_turn_into_successful_transaction(world):
    ledger, run = world
    with pytest.raises(DomainError, match="TRANSACTION_ROLLBACK_ONLY"):
        with ledger.store.transaction(run) as tx:
            tx.execute("INSERT INTO positions(run_id,account_id) VALUES(:r,'MISSING:USD')")
            try:
                tx.execute("SELECT 1").scalar_one()
            except DBAPIError:
                pass
    assert independent_audit(ledger.store, run)["pass"]


def test_terminated_owned_backend_invalidates_pool_and_retry_recovers_once(world):
    ledger, run = world
    store = ledger.store
    before = replay_check(store, run)
    with pytest.raises(DBAPIError):
        with store.transaction(run) as tx:
            ledger.submit(run, PaymentCommand(idempotency_key="p", principal_minor=10000))
            own_backend = tx.execute("SELECT pg_backend_pid()").scalar_one()
            with store.engine.connect() as killer:
                assert killer.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": own_backend}
                ).scalar_one()
            tx.execute("SELECT 1").scalar_one()
    assert replay_check(store, run)["state_hash"] == before["state_hash"]
    result = ledger.submit(run, PaymentCommand(idempotency_key="p", principal_minor=10000))
    assert result["economic_status"] == "SETTLED"
    ledger.submit(run, PaymentCommand(idempotency_key="p", principal_minor=10000))
    with store.transaction(run) as tx:
        assert tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id='p'")["n"] == 2
    assert independent_audit(store, run)["pass"] and replay_check(store, run)["match"]
