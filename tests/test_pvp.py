from concurrent.futures import ThreadPoolExecutor

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.exc import DBAPIError

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand
from railstorm.ledger import Ledger
from railstorm.store import Store

GOLDEN = {
    "ATLAS_US:USD": 499_980_000,
    "STERLING_UK:USD": 700_000_000,
    "STERLING_UK:SGD": 335_000_000,
    "MERIDIAN_SG:SGD": 765_000_000,
    "OPERATOR:USD": 20_000,
}


def test_golden_lost_ack_retry_reopen(world):
    ledger, run = world
    cmd = PaymentCommand(idempotency_key="golden")
    result = ledger.submit(run, cmd, drop_ack=True)
    assert result["economic_status"] == "SETTLED" and result["ack_status"] == "MISSING"
    other = Ledger(Store())
    for _ in range(5):
        assert other.submit(run, cmd, drop_ack=True) == result
    with other.store.transaction(run) as tx:
        for account, expected in GOLDEN.items():
            assert tx.position(account)["posted"] == expected
        for currency, expected in [("USD", 1_200_000_000), ("SGD", 1_100_000_000)]:
            row = tx.one(
                """SELECT sum(p.posted) n FROM positions p JOIN accounts a
            ON p.run_id=a.run_id AND p.account_id=a.id WHERE p.run_id=:r AND a.kind='CLAIM' AND a.currency=:c""",
                c=currency,
            )
            assert row["n"] == expected
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id='golden'")["n"] == 2
        )
    report = reconcile(other.store, run)
    assert report["financial_errors"] == 0
    assert [f["invariant"] for f in report["findings"]] == ["INV-09"]
    other.acknowledge(run, "golden")
    assert reconcile(other.store, run)["findings"] == []
    other.store.engine.dispose()


@pytest.mark.parametrize(
    "config,reason",
    [
        ({"funding": {"STERLING_UK:SGD": 500_000_000}}, "INSUFFICIENT_LIQUIDITY"),
        ({"funding": {"ATLAS_US:USD": 500_000_000}}, "INSUFFICIENT_LIQUIDITY"),
        ({"offline_banks": ["STERLING_UK"]}, "PARTICIPANT_DOWN"),
    ],
)
def test_no_leg_on_failed_prepare(store, config, reason):
    import uuid

    run = uuid.uuid4().hex
    ledger = Ledger(store)
    ledger.initialize(run, config)
    result = ledger.submit(run, PaymentCommand(idempotency_key="negative"))
    assert result["economic_status"] == "REJECTED" and result["reason"] == reason
    with store.transaction(run) as tx:
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id IS NOT NULL")["n"]
            == 0
        )
        assert tx.one("SELECT count(*) n FROM reservations WHERE run_id=:r")["n"] == 0
    assert reconcile(store, run)["financial_errors"] == 0


def test_expiry_releases_both_reservations(world):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="expires"))
    assert ledger.settle(run, "expires", 30_000)["economic_status"] == "EXPIRED"
    with ledger.store.transaction(run) as tx:
        assert tx.position("ATLAS_US:USD")["posted"] == 1_000_000_000
        assert tx.position("STERLING_UK:SGD")["posted"] == 1_000_000_000
        assert tx.one("SELECT sum(reserved) n FROM positions WHERE run_id=:r")["n"] == 0
    assert reconcile(ledger.store, run)["financial_errors"] == 0


def test_transaction_failure_after_first_leg_rolls_back(world):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="rollback"))
    # A database trigger injects a failure on the destination journal for this test run only.
    from sqlalchemy import text

    with ledger.store.engine.begin() as conn:
        conn.execute(
            text("""CREATE OR REPLACE FUNCTION test_fail_leg() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.id='rollback:target' THEN RAISE EXCEPTION 'INJECTED_CRASH'; END IF; RETURN NEW; END; $$""")
        )
        conn.execute(
            text(
                "CREATE TRIGGER test_fail BEFORE INSERT ON journal_entries FOR EACH ROW EXECUTE FUNCTION test_fail_leg()"
            )
        )
    try:
        with pytest.raises(DBAPIError):
            ledger.settle(run, "rollback")
    finally:
        with ledger.store.engine.begin() as conn:
            conn.execute(text("DROP TRIGGER test_fail ON journal_entries"))
            conn.execute(text("DROP FUNCTION test_fail_leg()"))
    with ledger.store.transaction(run) as tx:
        assert tx.payment("rollback")["economic_status"] == "PREPARED"
        assert tx.position("ATLAS_US:USD")["posted"] == 1_000_000_000
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id='rollback'")["n"]
            == 0
        )
    ledger.settle(run, "rollback")
    assert reconcile(ledger.store, run)["financial_errors"] == 0


def test_database_rejects_unbalanced_and_history_mutation(world):
    ledger, run = world
    with pytest.raises(DBAPIError, match="UNBALANCED_JOURNAL"), ledger.store.transaction(run) as tx:
        tx.execute("INSERT INTO journal_entries VALUES(:r,'bad',NULL,'USD','SHARED')")
        tx.execute("INSERT INTO journal_lines VALUES(:r,'bad',0,'ATLAS_US:USD','USD','SHARED',1)")
    with pytest.raises(DBAPIError, match="APPEND_ONLY"), ledger.store.transaction(run) as tx:
        tx.execute("UPDATE events SET event_type='FAKE' WHERE run_id=:r AND seq=1")


def test_no_reservation_release_after_consumption(world):
    ledger, run = world
    ledger.submit(run, PaymentCommand(idempotency_key="consumed"))
    with pytest.raises(DBAPIError, match="ILLEGAL_RESERVATION_TRANSITION"):
        with ledger.store.transaction(run) as tx:
            tx.execute("UPDATE reservations SET status='RELEASED' WHERE run_id=:r")


def test_concurrent_golden(world):
    ledger, run = world
    cmd = PaymentCommand(idempotency_key="parallel")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ledger.submit(run, cmd), range(12)))
    assert all(r["settlement_id"] == "settlement:parallel" for r in results)
    assert reconcile(ledger.store, run)["financial_errors"] == 0


@given(st.lists(st.integers(1, 100_000_000), min_size=1, max_size=8))
@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_random_payment_streams(store, amounts):
    import uuid

    ledger = Ledger(store)
    run = uuid.uuid4().hex
    ledger.initialize(run)
    for i, amount in enumerate(amounts):
        cmd = PaymentCommand(idempotency_key=f"p{i}", principal_minor=amount)
        ledger.submit(run, cmd)
        ledger.submit(run, cmd)
        assert reconcile(store, run)["financial_errors"] == 0
    assert replay_check(store, run)["match"]
