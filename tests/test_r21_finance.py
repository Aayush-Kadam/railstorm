"""Version ordering, source-final protection and full native shortage records."""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import text

from railstorm.audit import reconcile, replay_check, snapshot
from railstorm.domain import DomainError, PaymentCommand
from railstorm.ledger import Ledger
from railstorm.market_quotes import replace
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.correspondent import ChainCommand, CorrespondentRail
from railstorm.rails.instant_link import InstantLink
from railstorm.routing import Intent, Network, Router
from railstorm.store import Store


def capture(store, run):
    with store.transaction(run) as tx:
        state = snapshot(tx)
    result = {
        "state": state,
        "native": reconcile(store, run),
        "independent": independent_audit(store, run),
        "replay": replay_check(store, run),
    }
    assert (
        result["native"]["financial_errors"] == 0
        and result["independent"]["pass"]
        and result["replay"]["match"]
    )
    events: list[dict] = []
    while True:
        rows = store.events(run, events[-1]["seq"] if events else 0, 512)
        if not rows:
            break
        events.extend(rows)
    result["events"] = events
    return result


def save(name, value):
    out = Path(__file__).resolve().parents[1] / "docs/r21_evidence"
    out.mkdir(exist_ok=True)
    (out / (name + ".json")).write_text(json.dumps(value, indent=2))


def update(store, run, version, at=10, fee=25):
    return replace(
        store,
        run,
        pair="USD/SGD",
        version=version,
        numerator=133 if version == 1 else 140,
        denominator=100,
        at_ms=at,
        expires_ms=1000,
        fee_minor=fee,
    )


@pytest.mark.parametrize("update_first", [False, True])
def test_quote_update_execution_race(store, update_first):
    run = "r21-race-" + uuid.uuid4().hex
    Ledger(store).initialize(run, {"fee_minor": 25})
    update(store, run, 1, 0)
    opening = capture(store, run)

    def worker(updating):
        independent = Store()
        try:
            if updating:
                return update(independent, run, 2)
            return Ledger(independent).submit(
                run, PaymentCommand(idempotency_key="p", principal_minor=10000), 10
            )
        finally:
            independent.engine.dispose()

    waits = []

    def blocked(n):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with store.engine.connect() as conn:
                count = conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%FROM runs%'"
                    )
                ).scalar_one()
            waits.append(count)
            if count >= n:
                return
            time.sleep(0.02)
        raise AssertionError("Expected overlapping run-lock waiters")

    with store.engine.connect() as gate:
        transaction = gate.begin()
        gate.execute(text("SELECT id FROM runs WHERE id=:r FOR UPDATE"), {"r": run})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker, update_first)
            blocked(1)
            second = pool.submit(worker, not update_first)
            blocked(2)
            transaction.commit()
            results = [first.result(timeout=20), second.result(timeout=20)]
    payment = next(r for r in results if "economic_status" in r)
    assert payment["economic_status"] == "SETTLED"
    # prepare and settle are separate serialized transactions; a prepared quote remains binding.
    assert payment["quote"]["version"] == (2 if update_first else 1)
    assert payment["quote"]["output_minor"] == (14000 if update_first else 13300)
    before = capture(store, run)
    update(store, run, 3, 11)
    assert Ledger(store).settle(run, "p", 12)["quote"] == payment["quote"]
    final = capture(store, run)
    save(
        "CQ-order-" + str(update_first),
        {
            "opening": opening,
            "before_later_update": before,
            "final": final,
            "observed_waiters": waits,
            "results": results,
        },
    )


def test_route_quote_and_fee_change_requires_new_decision(store):
    world, run = "r21-world-" + uuid.uuid4().hex, "r21-route-" + uuid.uuid4().hex
    Ledger(store).initialize(world, {"fee_minor": 25})
    update(store, world, 1, 0)
    router = Router(store)
    router.initialize(run)
    network = Network(worlds={"ATOMIC_PVP": world})
    router.evaluate(run, Intent(payment_id="old", idempotency_key="old"), network)
    update(store, world, 2, 10, 30)
    with pytest.raises(DomainError, match="EXECUTION_QUOTE_CHANGED"):
        router.execute(run, "old", 10)
    router.evaluate(run, Intent(payment_id="new", idempotency_key="new"), network, 10)
    state = router.execute(run, "new", 11)["payment"]
    assert state["quote"]["fee_minor"] == 30 and state["quote"]["output_minor"] == 14000
    save("CQ-stale-route", {"world": capture(store, world), "routing": capture(store, run)})


def test_source_final_obligation_keeps_original_quote(store):
    run = "r21-sf-" + uuid.uuid4().hex
    rail = InstantLink(store)
    rail.initialize(run)
    update(store, run, 1, 0)
    cmd = PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
    rail.prepare(run, cmd)
    opening = capture(store, run)
    rail.deliver(run, "p", "SOURCE", 10)
    rail.deliver(run, "p", "DESTINATION", 30, available=False)
    pending = capture(store, run)
    assert pending["state"]["payments"]["p"]["obligation"]["amount_minor"] == 13300
    update(store, run, 2, 31)
    with pytest.raises(DomainError, match="IDEMPOTENCY_KEY_REUSED"):
        rail.prepare(run, cmd.model_copy(update={"principal_minor": 10001}), 32)
    assert rail.prepare(run, cmd, 32)["quote"]["version"] == 1
    after_update = capture(store, run)
    assert (
        after_update["state"]["payments"]["p"]["obligation"]
        == pending["state"]["payments"]["p"]["obligation"]
    )
    restarted = InstantLink(store)
    restarted.recover(run, "p", 40)
    restarted.recover(run, "p", 41)
    final = capture(store, run)
    assert final["state"]["positions"]["MERIDIAN_SG:SGD"]["posted"] == 13300
    assert final["state"]["payments"]["p"]["quote"]["version"] == 1
    assert len([j for j in final["state"]["journals"].values() if j["payment_id"] == "p"]) == 2
    save(
        "SF-01-08",
        {"opening": opening, "pending": pending, "after_market_update": after_update, "final": final},
    )


def test_dynamic_instant_routing_rejects_unadvertised_terms(store):
    world, run = "r21-il-" + uuid.uuid4().hex, "r21-route-" + uuid.uuid4().hex
    InstantLink(store).initialize(world)
    router = Router(store)
    router.initialize(run)
    network = Network(worlds={"INSTANT_LINK": world})
    router.evaluate(run, Intent(payment_id="old", idempotency_key="old"), network)
    update(store, world, 1, 0)
    with pytest.raises(DomainError, match="EXECUTION_PREFLIGHT_CHANGED"):
        router.execute(run, "old", 10)
    new = router.evaluate(run, Intent(payment_id="new", idempotency_key="new"), network, 10)
    assert new["evaluation"]["selected"] is None
    assert capture(store, world)["state"]["payments"] == {}


def test_correspondent_shortage_restore_preserves_prior_hop(store):
    run = "r21-ls-cb-" + uuid.uuid4().hex
    rail = CorrespondentRail(store)
    rail.initialize(run)
    opening = capture(store, run)
    with store.transaction(run) as tx:
        amount = tx.position("VOSTRO:SR_USD")["posted"] - 1
        tx.execute("UPDATE positions SET buffer=:b WHERE run_id=:r AND account_id='VOSTRO:SR_USD'", b=amount)
        tx.event("BUFFER_CHANGED", "VOSTRO:SR_USD", {"account_id": "VOSTRO:SR_USD", "buffer": amount})
    rail.create(run, ChainCommand(idempotency_key="p"))
    stages = []
    for t in range(50, 551, 50):
        rail.advance(run, "p", t)
        stages.append(capture(store, run))
    pending = stages[-1]
    assert pending["state"]["payments"]["p"]["reason"] == "INSUFFICIENT_CORRESPONDENT_LIQUIDITY"
    prior = {k: v for k, v in pending["state"]["journals"].items() if v["payment_id"] == "p"}
    with store.transaction(run) as tx:
        tx.execute("UPDATE positions SET buffer=0 WHERE run_id=:r AND account_id='VOSTRO:SR_USD'")
        tx.event("BUFFER_CHANGED", "VOSTRO:SR_USD", {"account_id": "VOSTRO:SR_USD", "buffer": 0})
    for t in range(600, 1601, 50):
        rail.advance(run, "p", t)
    final = capture(store, run)
    assert final["state"]["payments"]["p"]["economic_status"] == "SETTLED"
    assert all(final["state"]["journals"][k] == v for k, v in prior.items())
    save("LS-correspondent", {"opening": opening, "stages": stages, "final": final})


def test_destination_shortage_then_funded_new_attempt(store):
    run = "r21-ls-il-" + uuid.uuid4().hex
    rail = InstantLink(store)
    rail.initialize(run, provider_sgd=100)
    opening = capture(store, run)
    cmd = PaymentCommand(idempotency_key="rejected", principal_minor=10000, requested_rail="INSTANT_LINK")
    assert rail.prepare(run, cmd)["economic_status"] == "REJECTED"
    rejected = capture(store, run)
    assert rejected["state"]["positions"]["MERIDIAN_SG:SGD"]["posted"] == 0
    with store.transaction(run) as tx:
        tx.journal(
            "synthetic-liquidity-restoration",
            None,
            "SGD",
            [("OPENING:SGD", -20000), ("STERLING_UK:SGD", 20000)],
            issuer="SG_FAST",
        )
    rail.prepare(run, cmd.model_copy(update={"idempotency_key": "new"}), 1)
    rail.deliver(run, "new", "SOURCE", 11)
    rail.deliver(run, "new", "DESTINATION", 31, available=False)
    pending = capture(store, run)
    rail.recover(run, "new", 40)
    rail.recover(run, "new", 41)
    final = capture(store, run)
    assert final["state"]["positions"]["MERIDIAN_SG:SGD"]["posted"] == 13300
    assert final["state"]["payments"]["rejected"]["economic_status"] == "REJECTED"
    save("LS-destination", {"opening": opening, "rejected": rejected, "pending": pending, "final": final})
