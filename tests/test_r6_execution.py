"""Quote read-your-writes and combined reservation write failure boundaries."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand
from railstorm.ledger import Ledger
from railstorm.market_quotes import current, replace
from railstorm.p2a_oracle import independent_audit


def test_quote_view_merges_buffer_and_persisted_without_flushing(world):
    ledger, run = world
    store = ledger.store
    replace(store, run, pair="USD/SGD", version=1, numerator=133, denominator=100, at_ms=0, expires_ms=1000)
    with store.transaction(run) as tx:
        tx.event("DIAGNOSTIC", "test", {"irrelevant": True})
        count = len(tx.event_rows)
        assert current(tx, "USD", "SGD")["version"] == 1
        assert len(tx.event_rows) == count
        replace(
            store, run, pair="USD/SGD", version=2, numerator=140, denominator=100, at_ms=1, expires_ms=1000
        )
        replace(
            store, run, pair="EUR/SGD", version=1, numerator=145, denominator=100, at_ms=1, expires_ms=1000
        )
        assert current(tx, "USD", "SGD")["version"] == 2
        assert current(tx, "EUR", "SGD")["numerator"] == 145
        assert current(tx, "USD", "INR") is None
        # Generic event readers still force persistence and see all own writes.
        assert tx.one("SELECT count(*) n FROM events WHERE run_id=:r AND event_type='MARKET_QUOTE'")["n"] == 3
        assert current(tx, "USD", "SGD")["version"] == 2


def test_combined_reservation_failure_rolls_back_projection_and_history(world):
    ledger, run = world
    store = ledger.store
    with store.transaction(run) as tx:
        before = tx.position("ATLAS_US:USD")
        head = tx.run["next_seq"]
    with pytest.raises(IntegrityError):
        with store.transaction(run) as tx:
            tx.reserve("duplicate", "nonpayment", "ATLAS_US:USD", 100, 1000)
            tx.reserve("duplicate", "nonpayment", "ATLAS_US:USD", 100, 1000)
    with store.transaction(run) as tx:
        assert tx.position("ATLAS_US:USD") == before
        assert tx.run["next_seq"] == head
        assert tx.one("SELECT count(*) n FROM reservations WHERE run_id=:r")["n"] == 0
    assert reconcile(store, run)["financial_errors"] == 0
    assert replay_check(store, run)["match"]
    assert independent_audit(store, run)["pass"]


def test_quote_read_does_not_cache_projection_or_hide_unit_corruption(store):
    run = "r6-corrupt-" + uuid.uuid4().hex
    ledger = Ledger(store)
    ledger.initialize(run)
    ledger.submit(run, PaymentCommand(idempotency_key="p", principal_minor=10000))
    with store.transaction(run) as tx:
        assert current(tx, "USD", "SGD") is None
    with store.engine.begin() as conn:
        conn.execute(
            text("UPDATE positions SET posted=posted+1 WHERE run_id=:r AND account_id='ATLAS_US:USD'"),
            {"r": run},
        )
    assert reconcile(store, run)["financial_errors"] > 0
    assert not replay_check(store, run)["match"]
    assert not independent_audit(store, run)["pass"]
