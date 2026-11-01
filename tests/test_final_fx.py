"""Immutable scheduled atomic quotes preserve booked amounts across replacements."""

import uuid

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand
from railstorm.ledger import Ledger
from railstorm.p2a_oracle import independent_audit
from railstorm.store import Store


def test_quote_replacement_expiry_and_immutable_commitment():
    store = Store()
    ledger = Ledger(store)
    run = "final-fx-" + uuid.uuid4().hex
    schedule = [
        {
            "id": "market-v1",
            "version": 1,
            "pair": "USD/SGD",
            "numerator": 133,
            "denominator": 100,
            "valid_from_ms": 0,
            "expires_ms": 10,
        },
        {
            "id": "market-v2",
            "version": 2,
            "pair": "USD/SGD",
            "numerator": 140,
            "denominator": 100,
            "valid_from_ms": 10,
            "expires_ms": 20,
        },
    ]
    ledger.initialize(run, {"fee_minor": 25, "quote_schedule": schedule})
    first = ledger.prepare(run, PaymentCommand(idempotency_key="first", principal_minor=10001), 0)
    assert first["quote"]["version"] == 1 and first["quote"]["output_minor"] == 13301
    assert ledger.settle(run, "first", 1)["economic_status"] == "SETTLED"
    ledger.acknowledge(run, "first")
    ledger.prepare(run, PaymentCommand(idempotency_key="stale", principal_minor=10001), 2)
    assert ledger.settle(run, "stale", 10)["economic_status"] == "EXPIRED"
    second = ledger.prepare(run, PaymentCommand(idempotency_key="second", principal_minor=10001), 10)
    assert second["quote"]["version"] == 2 and second["quote"]["output_minor"] == 14001
    ledger.settle(run, "second", 11)
    ledger.acknowledge(run, "second")
    assert ledger.settle(run, "first", 12)["quote"] == first["quote"]
    assert (
        ledger.prepare(run, PaymentCommand(idempotency_key="expired", principal_minor=10001), 20)[
            "economic_status"
        ]
        == "REJECTED"
    )
    assert reconcile(store, run)["financial_errors"] == 0
    assert independent_audit(store, run)["pass"]
    assert replay_check(store, run)["match"]
