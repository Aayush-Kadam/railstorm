import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from railstorm.audit import reconcile, replay_check
from railstorm.domain import DomainError
from railstorm.rails.correspondent import ChainCommand, CorrespondentRail, default_relationships
from railstorm.scenarios import Fault
from railstorm.simulation import apply_fault


@pytest.fixture
def cb(store):
    run = uuid.uuid4().hex
    rail = CorrespondentRail(store)
    rail.initialize(run)
    return rail, run


def finish(rail, run, key, start=50, end=1500):
    for at in range(start, end + 1, 50):
        rail.advance(run, key, at)


def test_healthy_multihop_independent_balances(cb):
    rail, run = cb
    rail.create(run, ChainCommand(idempotency_key="healthy"))
    finish(rail, run, "healthy")
    with rail.store.transaction(run) as tx:
        assert tx.payment("healthy")["economic_status"] == "SETTLED"
        for account, expected in {
            "VOSTRO:AS_USD": 990000,
            "NOSTRO:AS_USD": -990000,
            "VOSTRO:SR_USD": 990100,
            "NOSTRO:SR_USD": -990100,
            "VOSTRO:RM_USD": 990150,
            "NOSTRO:RM_USD": -990150,
            "GL:ATLAS_US:PAYMENT_EXPENSE:USD": -10000,
            "GL:STERLING_UK:FEES:USD": 100,
            "GL:RHINE_EU:FEES:USD": 50,
            "GL:MERIDIAN_SG:FEES:USD": 25,
            "BENEFICIARY:healthy:USD": 9825,
        }.items():
            assert tx.position(account)["posted"] == expected
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id='healthy'")["n"]
            == 6
        )
        assert tx.one("SELECT count(*) n FROM cb_obligations WHERE run_id=:r AND status='OPEN'")["n"] == 0
    assert reconcile(rail.store, run)["financial_errors"] == 0
    assert replay_check(rail.store, run)["match"]


def test_downstream_exposure_and_recovery(cb):
    rail, run = cb
    rail.create(run, ChainCommand(idempotency_key="exposed"))
    rail.advance(run, "exposed", 100)
    rail.advance(run, "exposed", 150)
    fault = Fault(id="rhine-down", type="PARTICIPANT_DOWN", target="RHINE_EU", at_ms=151, duration_ms=649)
    apply_fault(rail.store, run, fault, True, 151)
    state = rail.advance(run, "exposed", 200)
    assert state["economic_status"] == "PENDING_EXPOSURE"
    report = reconcile(rail.store, run)
    assert report["financial_errors"] == 0
    assert report["correspondent"]["exposure_by_currency"]["USD"]["amount_minor"] == 9900
    with rail.store.transaction(run) as tx:
        assert tx.position("VOSTRO:AS_USD")["posted"] == 990000
        assert tx.position("TRANSIT:exposed:0:USD")["posted"] == 9900
        assert tx.position("VOSTRO:SR_USD")["posted"] == 1000000
    apply_fault(rail.store, run, fault, False, 800)
    finish(rail, run, "exposed", 800, 1200)
    assert reconcile(rail.store, run)["financial_errors"] == 0
    with rail.store.transaction(run) as tx:
        assert tx.position("BENEFICIARY:exposed:USD")["posted"] == 9825


def test_first_intermediary_down_no_posting(cb):
    rail, run = cb
    rail.create(run, ChainCommand(idempotency_key="first-down"))
    apply_fault(
        rail.store,
        run,
        Fault(id="down", type="PARTICIPANT_DOWN", target="STERLING_UK", at_ms=1, duration_ms=1000),
        True,
        1,
    )
    rail.advance(run, "first-down", 100)
    with rail.store.transaction(run) as tx:
        assert tx.position("VOSTRO:AS_USD")["posted"] == 1000000
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id='first-down'")["n"]
            == 0
        )


def test_duplicate_hop_and_missing_ack(cb):
    rail, run = cb
    rail.create(run, ChainCommand(idempotency_key="duplicate"))
    state = rail.advance(run, "duplicate", 100)
    assert state["next_stage"] == 1
    before = replay_check(rail.store, run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: rail.advance(run, "duplicate", 100, hop_index=0), range(10)))
    assert replay_check(rail.store, run) == before
    finish(rail, run, "duplicate", 150, 1000)
    assert reconcile(rail.store, run)["financial_errors"] == 0


def test_insufficient_onward_liquidity_stays_exposed(store):
    rels = default_relationships()
    rels[1] = rels[1].model_copy(update={"funding_minor": 1})
    rels[2] = rels[2].model_copy(update={"funding_minor": 1})
    rail, run = CorrespondentRail(store), uuid.uuid4().hex
    rail.initialize(run, rels)
    rail.create(run, ChainCommand(idempotency_key="dry"))
    finish(rail, run, "dry")
    with store.transaction(run) as tx:
        assert tx.payment("dry")["reason"] == "INSUFFICIENT_CORRESPONDENT_LIQUIDITY"
        assert tx.position("TRANSIT:dry:0:USD")["posted"] == 9900
    assert reconcile(store, run)["financial_errors"] == 0


def test_cutoff_and_directed_eligibility(store):
    rels = default_relationships()
    rels[0] = rels[0].model_copy(update={"cutoff_ms": 50})
    rail, run = CorrespondentRail(store), uuid.uuid4().hex
    rail.initialize(run, rels)
    rail.create(run, ChainCommand(idempotency_key="cutoff", deadline_ms=200))
    finish(rail, run, "cutoff", 50, 300)
    with store.transaction(run) as tx:
        assert tx.payment("cutoff")["economic_status"] == "EXPIRED"
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id='cutoff'")["n"] == 0
        )
    with pytest.raises(DomainError, match="NO_ELIGIBLE"):
        rail.create(
            run,
            ChainCommand(
                idempotency_key="reverse", sender_bank_id="MERIDIAN_SG", receiver_bank_id="ATLAS_US"
            ),
            300,
        )


def test_explicit_compensation_and_finality_boundary(cb):
    rail, run = cb
    rail.create(run, ChainCommand(idempotency_key="return"))
    rail.advance(run, "return", 100)
    rail.return_first_hop(run, "return", 101)
    before = replay_check(rail.store, run)
    rail.return_first_hop(run, "return", 102)
    assert replay_check(rail.store, run) == before
    with rail.store.transaction(run) as tx:
        assert tx.position("VOSTRO:AS_USD")["posted"] == 999900
        assert tx.position("GL:ATLAS_US:PAYMENT_EXPENSE:USD")["posted"] == -100
        assert tx.position("GL:STERLING_UK:FEES:USD")["posted"] == 100
        assert (
            tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id='return'")["n"] == 4
        )
    assert reconcile(rail.store, run)["financial_errors"] == 0
    rail.create(run, ChainCommand(idempotency_key="final"), 102)
    finish(rail, run, "final", 150, 1000)
    with pytest.raises(DomainError, match="FINAL_BENEFICIARY"):
        rail.return_first_hop(run, "final", 1001)


def test_fx_obligation_and_later_stall(cb):
    rail, run = cb
    rail.create(run, ChainCommand.model_validate({"idempotency_key": "fx", "destination_currency": "SGD"}))
    finish(rail, run, "fx", 50, 200)
    apply_fault(
        rail.store,
        run,
        Fault(id="down", type="PARTICIPANT_DOWN", target="RHINE_EU", at_ms=201, duration_ms=1000),
        True,
        201,
    )
    finish(rail, run, "fx", 250, 500)
    report = reconcile(rail.store, run)
    assert report["financial_errors"] == 0
    assert report["correspondent"]["exposure_by_currency"]["SGD"]["amount_minor"] == 13167
    with rail.store.transaction(run) as tx:
        assert tx.position("TRANSIT:fx:0:USD")["posted"] == 0
        assert tx.position("TRANSIT:fx:1:SGD")["posted"] == 13167
    apply_fault(
        rail.store,
        run,
        Fault(id="down", type="PARTICIPANT_DOWN", target="RHINE_EU", at_ms=201, duration_ms=1000),
        False,
        1201,
    )
    finish(rail, run, "fx", 1250, 1500)
    with rail.store.transaction(run) as tx:
        assert tx.position("BENEFICIARY:fx:SGD")["posted"] == 13092
    assert reconcile(rail.store, run)["financial_errors"] == 0


@given(st.integers(1000, 50000))
@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_amounts_and_mirrors(store, amount):
    rail, run = CorrespondentRail(store), uuid.uuid4().hex
    rail.initialize(run)
    rail.create(run, ChainCommand(idempotency_key="property", principal_minor=amount))
    finish(rail, run, "property", 50, 500)
    with store.transaction(run) as tx:
        assert tx.position("BENEFICIARY:property:USD")["posted"] == amount - 175
        for rel in default_relationships():
            assert tx.position("NOSTRO:" + rel.id)["posted"] == -tx.position("VOSTRO:" + rel.id)["posted"]
    assert reconcile(store, run)["financial_errors"] == 0
