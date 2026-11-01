"""Independent P0.1 crash, reservation, quote and accounting regressions."""

import os
import subprocess
import sys
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from railstorm.audit import reconcile, replay_check
from railstorm.domain import DomainError, PaymentCommand
from railstorm.ledger import Ledger
from railstorm.scenarios import Scenario
from railstorm.simulation import create_run, run_scenario, step
from railstorm.store import Store, Transaction

GOLDEN = {
    "ATLAS_US:USD": 499_980_000,
    "STERLING_UK:USD": 700_000_000,
    "STERLING_UK:SGD": 335_000_000,
    "MERIDIAN_SG:SGD": 765_000_000,
    "OPERATOR:USD": 20_000,
}


def assert_golden(store, run):
    with store.transaction(run) as tx:
        for account, expected in GOLDEN.items():
            assert tx.position(account)["posted"] == expected
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id IS NOT NULL")["n"]
            == 2
        )
        assert tx.one("SELECT count(*) n FROM reservations WHERE run_id=:r AND status='ACTIVE'")["n"] == 0
    assert reconcile(store, run)["financial_errors"] == 0
    assert replay_check(store, run)["match"]


def test_crash_before_commit_decision(world, monkeypatch):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="predecision"))
    original = Transaction.decision

    def crash(tx, payment, decision):
        if decision == "COMMIT":
            raise RuntimeError("CRASH_BEFORE_DECISION")
        return original(tx, payment, decision)

    with monkeypatch.context() as scoped:
        scoped.setattr(Transaction, "decision", crash)
        with pytest.raises(RuntimeError, match="CRASH_BEFORE_DECISION"):
            ledger.settle(run, "predecision")
    with ledger.store.transaction(run) as tx:
        assert tx.payment("predecision")["economic_status"] == "PREPARED"
        assert tx.position("ATLAS_US:USD")["reserved"] == 500_020_000
        assert tx.position("STERLING_UK:SGD")["reserved"] == 665_000_000
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id IS NOT NULL")["n"]
            == 0
        )
    ledger.submit(run, PaymentCommand(idempotency_key="predecision"))
    assert_golden(ledger.store, run)


def test_caught_nested_command_failure_cannot_commit_partial_effects(world, monkeypatch):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="caught"))
    before = replay_check(ledger.store, run)
    original = Transaction.decision

    def crash(tx, payment, decision):
        if decision == "COMMIT":
            raise RuntimeError("CAUGHT_FAILURE")
        return original(tx, payment, decision)

    with monkeypatch.context() as scoped:
        scoped.setattr(Transaction, "decision", crash)
        with pytest.raises(DomainError, match="TRANSACTION_ROLLBACK_ONLY"):
            with ledger.store.transaction(run):
                try:
                    ledger.settle(run, "caught")
                except RuntimeError:
                    pass
    assert replay_check(ledger.store, run) == before
    ledger.submit(run, PaymentCommand(idempotency_key="caught"))
    assert_golden(ledger.store, run)


WORKER = """
import os,sys
from railstorm.store import Store
from railstorm.ledger import Ledger
from railstorm.domain import PaymentCommand
from railstorm.simulation import step
s=Store(); run=sys.argv[1]; mode=sys.argv[2]
if mode=='before':
    with s.transaction(run):
        Ledger(s).submit(run,PaymentCommand(idempotency_key='process'))
        os._exit(17)
elif mode=='batch':
    with s.transaction(run):
        step(s,run,3)
        os._exit(19)
else:
    Ledger(s).submit(run,PaymentCommand(idempotency_key='process'),drop_ack=True)
    os._exit(18)
"""


@pytest.mark.parametrize("mode,exitcode", [("before", 17), ("after", 18)])
def test_actual_process_exit_before_or_after_commit(world, mode, exitcode):
    ledger, run = world
    result = subprocess.run(
        [sys.executable, "-c", WORKER, run, mode], env=os.environ.copy(), capture_output=True, timeout=30
    )
    assert result.returncode == exitcode, result.stderr.decode(errors="replace")
    reopened = Store()
    with reopened.transaction(run) as tx:
        if mode == "before":
            assert tx.payment("process") is None
            assert tx.position("ATLAS_US:USD")["posted"] == 1_000_000_000
            assert tx.position("ATLAS_US:USD")["reserved"] == 0
        else:
            assert tx.payment("process")["economic_status"] == "SETTLED"
            assert tx.payment("process")["ack_status"] == "MISSING"
    for _ in range(3):
        Ledger(reopened).submit(run, PaymentCommand(idempotency_key="process"))
    assert_golden(reopened, run)
    reopened.engine.dispose()


def test_actual_process_exit_rolls_back_batch_then_replays(store):
    run = uuid.uuid4().hex
    scenario = Scenario(scenario_id="interrupted")
    create_run(store, run, scenario)
    step(store, run, 1)
    before = replay_check(store, run)
    result = subprocess.run(
        [sys.executable, "-c", WORKER, run, "batch"], env=os.environ.copy(), capture_output=True, timeout=30
    )
    assert result.returncode == 19, result.stderr.decode(errors="replace")
    assert replay_check(store, run)["state_hash"] == before["state_hash"]
    with store.transaction(run) as tx:
        assert tx.one("SELECT cursor FROM runtime WHERE run_id=:r")["cursor"] == 1
    assert step(store, run, 3)["done"]
    fresh = run_scenario(store, uuid.uuid4().hex, scenario)
    assert replay_check(store, run)["state_hash"] == fresh["verification"]["replay"]["state_hash"]
    assert replay_check(store, run)["event_hash"] == fresh["verification"]["replay"]["event_hash"]


def test_duplicate_ack_is_no_op(world):
    ledger, run = world
    ledger.submit(run, PaymentCommand(idempotency_key="ack"), drop_ack=True)
    ledger.acknowledge(run, "ack")
    before = replay_check(ledger.store, run)
    for _ in range(5):
        ledger.acknowledge(run, "ack")
    assert replay_check(ledger.store, run) == before
    assert_golden(ledger.store, run)


def test_abort_and_duplicate_release(world):
    ledger, run = world
    cmd = PaymentCommand(idempotency_key="abort")
    ledger.prepare(run, cmd)
    assert ledger.abort(run, "abort")["economic_status"] == "ABORTED"
    before = replay_check(ledger.store, run)
    ledger.abort(run, "abort")
    with ledger.store.transaction(run) as tx:
        tx.resolve("abort", "RELEASED")
        assert tx.position("ATLAS_US:USD")["reserved"] == 0
        assert tx.position("STERLING_UK:SGD")["reserved"] == 0
        assert tx.position("ATLAS_US:USD")["posted"] == 1_000_000_000
    assert replay_check(ledger.store, run) == before
    assert reconcile(ledger.store, run)["findings"] == []


def test_cannot_abort_final_commit(world):
    ledger, run = world
    ledger.submit(run, PaymentCommand(idempotency_key="final"))
    with pytest.raises(DomainError, match="COMMIT_CANNOT_ABORT"):
        ledger.abort(run, "final")
    assert_golden(ledger.store, run)


def test_missing_prepared_hold_blocks_settlement_and_audit(world):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="holds"))
    with ledger.store.transaction(run) as tx:
        tx.resolve("holds", "RELEASED")
    with pytest.raises(DomainError, match="RESERVATION_MISMATCH"):
        ledger.settle(run, "holds")
    assert reconcile(ledger.store, run)["financial_errors"] > 0


def test_tampered_quote_projection_blocks_settlement(world):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="quote"))
    with ledger.store.transaction(run) as tx:
        tx.execute("""UPDATE payments SET data=jsonb_set(data,'{quote,output_minor}','1')
          WHERE run_id=:r AND id='quote'""")
    with pytest.raises(DomainError, match="QUOTE_PROJECTION_MISMATCH"):
        ledger.settle(run, "quote")
    assert reconcile(ledger.store, run)["financial_errors"] > 0


def test_reconciler_detects_balanced_replay_consistent_wrong_fee(world, monkeypatch):
    ledger, run = world
    original = Transaction.journal

    def divert_fee(tx, identity, payment, currency, lines):
        changed = [
            ("RHINE_EU:USD" if account == "OPERATOR:USD" else account, amount) for account, amount in lines
        ]
        return original(tx, identity, payment, currency, changed)

    with monkeypatch.context() as scoped:
        scoped.setattr(Transaction, "journal", divert_fee)
        ledger.submit(run, PaymentCommand(idempotency_key="fee-diversion"))
    # Correct journal balancing and deterministic replay are insufficient to
    # establish that the accepted economic contract was actually fulfilled.
    assert replay_check(ledger.store, run)["match"]
    assert reconcile(ledger.store, run)["financial_errors"] > 0


def test_failed_initialization_leaves_no_ready_run(store):
    run = uuid.uuid4().hex
    with pytest.raises(DomainError, match="INVALID_FUNDING"):
        Ledger(store).initialize(run, {"funding": {"ATLAS_US:USD": -1}})
    with store.engine.connect() as conn:
        from sqlalchemy import text

        assert conn.execute(text("SELECT count(*) FROM runs WHERE id=:r"), {"r": run}).scalar_one() == 0


def test_buffered_outbox_failure_rolls_back_every_financial_effect(world, monkeypatch):
    ledger, run = world
    before = replay_check(ledger.store, run)
    from sqlalchemy import event

    def fail_outbox(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO outbox" in statement:
            raise RuntimeError("OUTBOX_FLUSH_FAILURE")

    with monkeypatch.context():
        event.listen(ledger.store.engine, "before_cursor_execute", fail_outbox)
        try:
            with pytest.raises(RuntimeError, match="OUTBOX_FLUSH_FAILURE"):
                ledger.submit(run, PaymentCommand(idempotency_key="flush"))
        finally:
            event.remove(ledger.store.engine, "before_cursor_execute", fail_outbox)
    assert replay_check(ledger.store, run) == before


def test_no_uncommitted_event_is_visible_and_payload_is_frozen(world):
    ledger, run = world
    before = len(ledger.store.events(run, limit=1000))
    with ledger.store.transaction(run) as tx:
        payload = {"value": 1}
        tx.event("TEST_DIAGNOSTIC", "diagnostic", payload)
        payload["value"] = 999
        assert len(ledger.store.events(run, limit=1000)) == before
        # The explicit transaction read must see its own pending records.
        assert tx.one("SELECT payload FROM events WHERE run_id=:r ORDER BY seq DESC LIMIT 1")["payload"] == {
            "value": 1
        }
    assert len(ledger.store.events(run, limit=1000)) == before + 1
    assert replay_check(ledger.store, run)["match"]


def test_cached_payment_copy_cannot_change_final_economics(world):
    ledger, run = world
    ledger.submit(run, PaymentCommand(idempotency_key="cached"))
    with ledger.store.transaction(run) as tx:
        proposed = tx.payment("cached")
        proposed["quote"]["output_minor"] = 1
        assert tx.payment("cached")["quote"]["output_minor"] == 665_000_000
        with pytest.raises(DomainError, match="FINAL_SETTLEMENT_IMMUTABLE"):
            tx.payment_state("cached", proposed)
    assert_golden(ledger.store, run)


def test_equal_time_fault_restore_and_arrival_observe_updated_fault_view(store):
    run = uuid.uuid4().hex
    scenario = Scenario.model_validate(
        {
            "scenario_id": "equal-time",
            "payment_count": 3,
            "interval_ms": 10,
            "principal_minor": 10000,
            "fee_minor": 20,
            "faults": [{"id": "down", "type": "PARTICIPANT_DOWN", "at_ms": 0, "duration_ms": 10}],
        }
    )
    result = run_scenario(store, run, scenario)
    assert result["metrics"]["statuses"] == {"REJECTED": 1, "SETTLED": 2}
    assert result["verification"]["financial_errors"] == 0
    with store.transaction(run) as tx:
        at10 = tx.rows("SELECT event_type FROM events WHERE run_id=:r AND logical_time_ms=10 ORDER BY seq")
        assert at10[0]["event_type"] == "FAULT_RESTORED"


def test_prepare_failure_does_not_leave_one_hold(world, monkeypatch):
    ledger, run = world
    original = Transaction.reserve
    calls = 0

    def fail_second(tx, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("SECOND_RESERVATION_FAILURE")
        return original(tx, *args, **kwargs)

    before = replay_check(ledger.store, run)
    with monkeypatch.context() as scoped:
        scoped.setattr(Transaction, "reserve", fail_second)
        with pytest.raises(RuntimeError, match="SECOND_RESERVATION_FAILURE"):
            ledger.prepare(run, PaymentCommand(idempotency_key="one-hold"))
    assert replay_check(ledger.store, run) == before


@pytest.mark.parametrize(
    "currency,amount", [("GBP", 100), ("USD", 0), ("USD", -1), ("USD", True), ("USD", 1.0)]
)
def test_strict_command_rejects_invalid_money(currency, amount):
    with pytest.raises(ValidationError):
        PaymentCommand(idempotency_key="invalid", source_currency=currency, principal_minor=amount)


@given(st.lists(st.tuples(st.integers(1, 1_000_000), st.booleans()), min_size=1, max_size=12))
@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_random_commit_abort_retry_streams(store, operations):
    ledger, run = Ledger(store), uuid.uuid4().hex
    ledger.initialize(run, {"fee_minor": 7})
    principal_total = output_total = fee_total = 0
    for i, (amount, commit) in enumerate(operations):
        key = f"s{i}"
        cmd = PaymentCommand(idempotency_key=key, principal_minor=amount)
        ledger.prepare(run, cmd)
        if commit:
            ledger.submit(run, cmd, drop_ack=True)
            ledger.submit(run, cmd)
            principal_total += amount
            output_total += amount * 133 // 100
            fee_total += 7
        else:
            ledger.abort(run, key)
            ledger.abort(run, key)
        with store.transaction(run) as tx:
            assert tx.position("ATLAS_US:USD")["posted"] == 1_000_000_000 - principal_total - fee_total
            assert tx.position("STERLING_UK:USD")["posted"] == 200_000_000 + principal_total
            assert tx.position("STERLING_UK:SGD")["posted"] == 1_000_000_000 - output_total
            assert tx.position("MERIDIAN_SG:SGD")["posted"] == 100_000_000 + output_total
            assert tx.position("OPERATOR:USD")["posted"] == fee_total
        assert reconcile(store, run)["findings"] == []
