from concurrent.futures import ThreadPoolExecutor

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from railstorm.audit import reconcile, replay_check
from railstorm.domain import Currency, DomainError, Money, PaymentCommand, Scheduler, convert


def usd(key="initial-usd-0001", amount=500_000_000):
    return PaymentCommand(
        idempotency_key=key,
        destination_currency=Currency.USD,
        receiver_bank_id="STERLING_UK",
        principal_minor=amount,
    )


def test_initial_transfer_and_duplicate(world):
    ledger, run = world
    result = ledger.submit(run, usd())
    assert result["economic_status"] == "SETTLED"
    assert ledger.submit(run, usd()) == result
    with ledger.store.transaction(run) as tx:
        assert tx.position("ATLAS_US:USD")["posted"] == 500_000_000
        assert tx.position("STERLING_UK:USD")["posted"] == 700_000_000
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id IS NOT NULL")["n"]
            == 1
        )
    assert reconcile(ledger.store, run)["financial_errors"] == 0
    assert replay_check(ledger.store, run)["match"]


def test_key_misuse(world):
    ledger, run = world
    ledger.submit(run, usd())
    with pytest.raises(DomainError, match="IDEMPOTENCY_KEY_REUSED"):
        ledger.submit(run, usd(amount=1))


def test_concurrent_oversubscription(world):
    ledger, run = world
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda key: ledger.prepare(run, usd(key, 700_000_000)), ["a", "b"]))
    assert sorted(r["economic_status"] for r in results) == ["PREPARED", "REJECTED"]
    with ledger.store.transaction(run) as tx:
        assert tx.position("ATLAS_US:USD")["reserved"] == 700_000_000
    assert reconcile(ledger.store, run)["financial_errors"] == 0


def test_duplicate_concurrency(world):
    ledger, run = world
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ledger.submit(run, usd()), range(8)))
    assert all(r["economic_status"] == "SETTLED" for r in results)
    assert reconcile(ledger.store, run)["financial_errors"] == 0


@given(st.integers(min_value=0, max_value=9_000_000_000_000_000))
def test_money_exact(amount):
    assert Money(Currency.USD, amount).amount_minor == amount


@pytest.mark.parametrize("value", [True, 1.0, -1, float("nan"), 9_000_000_000_000_001])
def test_invalid_money(value):
    with pytest.raises(DomainError):
        Money(Currency.USD, value)


@given(st.integers(1, 10**10), st.integers(1, 1000), st.integers(1, 1000))
@settings(max_examples=100)
def test_rational_floor(amount, numerator, denominator):
    if amount * numerator < denominator:
        with pytest.raises(DomainError):
            convert(amount, numerator, denominator)
    else:
        result = convert(amount, numerator, denominator)
        assert result * denominator <= amount * numerator < (result + 1) * denominator


def test_stable_scheduler():
    q = Scheduler()
    q.push(10, 2, "later_priority", {})
    q.push(10, 1, "first", {})
    q.push(10, 1, "second", {})
    assert [q.pop()[3] for _ in range(3)] == ["first", "second", "later_priority"]
    with pytest.raises(DomainError):
        q.push(9, 0, "past", {})
