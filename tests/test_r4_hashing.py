import uuid

import pytest

from experiments.r4_benchmark import baseline, baseline_store
from railstorm.audit import reconcile, state_digest
from railstorm.domain import PaymentCommand, digest
from railstorm.ledger import Ledger
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.instant_link import InstantLink


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        None,
        {"z": '€\n\\"', "a": [True, False, None, 1, -2, 1.5]},
        {1: "integer keys", 2: {False: "boolean key"}},
        {"correspondent": {"hops": {str(i): {"values": [i, "é", {}]} for i in range(100)}}},
        {
            "journals": {
                str(i): {"lines": [{"account": "a", "delta": -i}, {"account": "b", "delta": i}]}
                for i in range(100)
            }
        },
    ],
)
def test_canonical_hash_bytes_identical(value):
    assert state_digest(value) == digest(value) == baseline().state_digest(value)


@pytest.mark.parametrize("rail", ["atomic", "instant"])
def test_old_new_full_audit_and_negative_projection(store, rail):
    run = "r4-diff-" + uuid.uuid4().hex
    if rail == "atomic":
        Ledger(store).initialize(run)
        Ledger(store).submit(run, PaymentCommand(idempotency_key="p", principal_minor=10000))
    else:
        link = InstantLink(store)
        link.initialize(run)
        link.prepare(
            run, PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
        )
        link.deliver(run, "p", "SOURCE", 10)
    original = baseline()
    assert original.reconcile(store, run) == reconcile(store, run)
    with store.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text("UPDATE positions SET posted=posted+1 WHERE run_id=:r AND account_id='ATLAS_US:USD'"),
            {"r": run},
        )
    previous, current = original.reconcile(store, run), reconcile(store, run)
    assert previous == current and current["financial_errors"] > 0 and not current["replay"]["match"]
    assert not independent_audit(store, run)["pass"]
    with store.engine.begin() as conn:
        conn.execute(
            text("UPDATE positions SET posted=posted-1 WHERE run_id=:r AND account_id='ATLAS_US:USD'"),
            {"r": run},
        )
    assert original.reconcile(store, run) == reconcile(store, run)
    assert independent_audit(store, run)["pass"]


def test_original_modified_event_tamper_diagnostics_identical(store, monkeypatch):
    import copy

    import railstorm.audit as modified

    run = "r4-event-" + uuid.uuid4().hex
    Ledger(store).initialize(run)
    Ledger(store).submit(run, PaymentCommand(idempotency_key="p", principal_minor=10000))
    previous = baseline()
    with store.transaction(run) as tx:
        actual = modified.snapshot(tx)
        events = list(modified.ordered_events(tx))
        changed = copy.deepcopy(events)
        financial = next(e for e in changed if e["event_type"] == "JOURNAL_POSTED")
        financial["payload"]["lines"][0]["delta"] += 1
        monkeypatch.setattr(previous, "ordered_events", lambda tx: iter(changed))
        monkeypatch.setattr(modified, "ordered_events", lambda tx: iter(changed))
        old_result = previous._replay_check(tx, actual)
        new_result = modified._replay_check(tx, actual)
        assert old_result == new_result and not new_result["match"]
        assert new_result["earliest_corrupt_event"] is not None


def test_original_modified_posting_groups_and_projections_identical(store):
    import railstorm.audit as audit

    old_store = baseline_store()
    states = []
    heads = []
    try:
        for instance in (old_store, store):
            run = "r4-posting-" + uuid.uuid4().hex
            ledger = Ledger(instance)
            ledger.initialize(run, {"fee_minor": 25})
            for i in range(5):
                ledger.submit(run, PaymentCommand(idempotency_key=f"p{i}", principal_minor=10001 + i))
            with instance.transaction(run) as tx:
                states.append(audit.snapshot(tx))
            report = reconcile(instance, run)
            assert report["financial_errors"] == 0 and report["replay"]["match"]
            assert independent_audit(instance, run)["pass"]
            heads.append(report["replay"]["event_hash"])
        assert states[0] == states[1] and heads[0] == heads[1]
    finally:
        old_store.engine.dispose()
