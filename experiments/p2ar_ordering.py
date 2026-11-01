"""Bounded reorderings supported by the durable instant-link message protocol."""

import json
import uuid
from pathlib import Path

from sqlalchemy import text

from railstorm.audit import reconcile, replay_check
from railstorm.domain import DomainError, PaymentCommand
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.instant_link import InstantLink
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]


def exercise(store):
    run = "p2ar-order-" + uuid.uuid4().hex
    rail = InstantLink(store)
    rail.initialize(run)
    command = PaymentCommand(idempotency_key="payment", principal_minor=10000, requested_rail="INSTANT_LINK")
    rail.prepare(run, command)
    trace = []

    def capture(action, disposition):
        state = rail.inspect(run, "payment")
        with store.engine.connect() as conn:
            journals = conn.execute(
                text("SELECT count(*) FROM journal_entries WHERE run_id=:r AND payment_id='payment'"),
                {"r": run},
            ).scalar_one()
            beneficiary = conn.execute(
                text("SELECT posted FROM positions WHERE run_id=:r AND account_id='MERIDIAN_SG:SGD'"),
                {"r": run},
            ).scalar_one()
        independent = independent_audit(store, run)
        native = reconcile(store, run)
        replay = replay_check(store, run)
        assert independent["pass"] and native["financial_errors"] == 0 and replay["match"]
        trace.append(
            {
                "action": action,
                "disposition": disposition,
                "payment": state["payment"],
                "messages": state["messages"],
                "journal_groups": journals,
                "beneficiary_posted_minor": beneficiary,
                "reservations": state["reservations"],
                "independent": independent,
                "native": native,
                "replay": replay,
            }
        )

    try:
        rail.deliver(run, "payment", "DESTINATION", 1)
    except DomainError as exc:
        assert exc.code == "MESSAGE_NOT_READY"
        capture("destination_before_source", "REJECTED:MESSAGE_NOT_READY")
    else:
        raise AssertionError("Out-of-order destination unexpectedly accepted")
    rail.deliver(run, "payment", "SOURCE", 10)
    early = rail.deliver(run, "payment", "DESTINATION", 20)
    assert early["delivery_status"] == "DELAYED"
    capture("destination_before_due", "DELAYED:obligation_retained")
    assert trace[-1]["journal_groups"] == 1 and trace[-1]["beneficiary_posted_minor"] == 0
    assert trace[-1]["payment"]["obligation"]["amount_minor"] == 13300
    lost = rail.deliver(run, "payment", "DESTINATION", 30, lose_ack=True)
    assert lost["ack_status"] == "LOST"
    capture("destination_committed_ack_lost", "COMMITTED:ACK_LOST")
    assert trace[-1]["journal_groups"] == 2 and trace[-1]["beneficiary_posted_minor"] == 13300
    retry = rail.deliver(run, "payment", "DESTINATION", 31)
    assert retry["ack_status"] == "ACKNOWLEDGED"
    capture("retry_then_ack", "ACKNOWLEDGED:prior_commit_discovered")
    old_source = rail.deliver(run, "payment", "SOURCE", 32)
    assert old_source["ack_status"] == "ACKNOWLEDGED"
    capture("older_source_after_destination_final", "IGNORED:already_acknowledged")
    duplicate = rail.deliver(run, "payment", "DESTINATION", 33)
    assert duplicate["ack_status"] == "ACKNOWLEDGED"
    capture("duplicate_destination", "IGNORED:already_acknowledged")
    rail.recover(run, "payment", 34)
    capture("repeat_recovery", "NO_FINANCIAL_EFFECT")
    assert all(x["journal_groups"] == 2 and x["beneficiary_posted_minor"] == 13300 for x in trace[2:])
    assert all(r["status"] == "CONSUMED" for r in trace[-1]["reservations"])
    return {
        "run_id": run,
        "supported_message_types": ["SOURCE", "DESTINATION"],
        "arbitrary_permutations_supported": False,
        "trace": trace,
        "final_journal_groups": trace[-1]["journal_groups"],
        "final_beneficiary_minor": trace[-1]["beneficiary_posted_minor"],
    }


if __name__ == "__main__":
    result = exercise(Store())
    (ROOT / "docs/P2_A_R_ordering.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Supported message ordering passed", len(result["trace"]))
