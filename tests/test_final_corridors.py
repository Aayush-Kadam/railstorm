"""Funded four-currency native atomic corridors, independently audited."""

import uuid

import pytest

from railstorm.audit import reconcile, replay_check
from railstorm.domain import Currency, PaymentCommand
from railstorm.ledger import Ledger
from railstorm.p2a_oracle import independent_audit
from railstorm.store import Store


@pytest.mark.parametrize(
    "source,target,sender,receiver,numerator,denominator",
    [
        (Currency.USD, Currency.SGD, "ATLAS_US", "MERIDIAN_SG", 133, 100),
        (Currency.USD, Currency.EUR, "ATLAS_US", "RHINE_EU", 92, 100),
        (Currency.USD, Currency.INR, "ATLAS_US", "BHARAT_IN", 83, 1),
        (Currency.EUR, Currency.SGD, "RHINE_EU", "MERIDIAN_SG", 145, 100),
    ],
)
def test_funded_atomic_corridor(source, target, sender, receiver, numerator, denominator):
    store = Store()
    ledger = Ledger(store)
    run = "final-corridor-" + uuid.uuid4().hex
    output = 10001 * numerator // denominator
    ledger.initialize(
        run,
        {
            "fee_minor": 25,
            "rates": {f"{source}/{target}": [numerator, denominator]},
            "funding": {f"{sender}:{source}": 100000, f"STERLING_UK:{target}": 2000000},
        },
    )
    command = PaymentCommand(
        idempotency_key="corridor",
        sender_bank_id=sender,
        receiver_bank_id=receiver,
        source_currency=source,
        destination_currency=target,
        principal_minor=10001,
    )
    prepared = ledger.prepare(run, command)
    assert prepared["quote"]["output_minor"] == output
    assert ledger.settle(run, "corridor", 1)["economic_status"] == "SETTLED"
    ledger.acknowledge(run, "corridor")
    ledger.settle(run, "corridor", 2)
    with store.transaction(run) as tx:
        assert tx.position(f"{sender}:{source}")["posted"] == 89974
        assert tx.position(f"OPERATOR:{source}")["posted"] == 25
        assert len(tx.rows("SELECT id FROM journal_entries WHERE run_id=:r AND payment_id='corridor'")) == 2
    assert reconcile(store, run)["financial_errors"] == 0
    assert replay_check(store, run)["match"]
    assert independent_audit(store, run)["pass"]
