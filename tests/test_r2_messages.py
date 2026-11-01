"""Named native message adversaries with durable state and three verification paths."""

import json
import uuid
from pathlib import Path

import pytest

from railstorm.audit import reconcile, replay_check
from railstorm.domain import DomainError, PaymentCommand
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.instant_link import InstantLink

OUT = Path(__file__).resolve().parents[1] / "docs/thunderbolt_r2_evidence"


def capture(store, rail, run, payment):
    result = {
        "inspection": rail.inspect(run, payment),
        "native": reconcile(store, run),
        "independent": independent_audit(store, run),
        "replay": replay_check(store, run),
    }
    assert result["native"]["financial_errors"] == 0
    assert result["independent"]["pass"] and result["replay"]["match"]
    return result


@pytest.mark.parametrize("scenario", [f"ADV-{i:02d}" for i in range(1, 8)])
def test_message_adversary(store, scenario):
    rail = InstantLink(store)
    run = "r2-msg-" + uuid.uuid4().hex
    rail.initialize(run, provider_sgd=100000, sender_usd=100000)
    command = PaymentCommand(idempotency_key="a", principal_minor=10000, requested_rail="INSTANT_LINK")
    rail.prepare(run, command)
    opening = capture(store, rail, run, "a")
    if scenario == "ADV-01":
        with pytest.raises(DomainError, match="MESSAGE_NOT_READY"):
            rail.deliver(run, "a", "DESTINATION", 1)
    rail.deliver(run, "a", "SOURCE", 10, lose_ack=scenario == "ADV-02")
    if scenario == "ADV-03":
        rail.deliver(run, "a", "SOURCE", 11)
    if scenario == "ADV-06":
        rail.prepare(run, command.model_copy(update={"idempotency_key": "b"}), 11)
        rail.deliver(run, "b", "SOURCE", 21)
    rail.deliver(run, "a", "DESTINATION", 30, available=False)
    intermediate = capture(store, rail, run, "a")
    assert intermediate["inspection"]["payment"]["obligation"]["amount_minor"] == 13300
    assert not intermediate["inspection"]["payment"]["destination_final"]
    if scenario == "ADV-06":
        rail.deliver(run, "b", "DESTINATION", 41)
    rail = InstantLink(store)
    rail.recover(run, "a", 50)
    if scenario in ("ADV-04", "ADV-05", "ADV-07"):
        rail.deliver(run, "a", "DESTINATION", 51)
        rail.deliver(run, "a", "SOURCE", 52)
    rail.recover(run, "a", 60)
    final = capture(store, rail, run, "a")
    assert final["inspection"]["payment"]["destination_final"]
    assert len(final["inspection"]["journals"]) == 2
    beneficiary = next(p for p in final["inspection"]["positions"] if p["account_id"] == "MERIDIAN_SG:SGD")
    assert beneficiary["posted"] == (26600 if scenario == "ADV-06" else 13300)
    OUT.mkdir(exist_ok=True)
    (OUT / f"{scenario}.json").write_text(
        json.dumps(
            {
                "id": scenario,
                "run_id": run,
                "expected": "one credit per payment; pending obligation retained until recovery",
                "opening": opening,
                "intermediate": intermediate,
                "final": final,
                "command": f"python -m pytest -q tests/test_r2_messages.py -k {scenario}",
            },
            indent=2,
        )
    )
