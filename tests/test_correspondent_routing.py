"""Twelve routed-correspondent financial integration fixtures."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from railstorm.audit import reconcile, replay_check
from railstorm.domain import Currency, DomainError
from railstorm.rails.correspondent import ChainCommand, CorrespondentRail, default_relationships
from railstorm.routing import Intent, Network, Policy, Router
from railstorm.scenarios import Fault
from railstorm.simulation import apply_fault

CASES = [f"CB-R{i:02d}" for i in range(1, 13)]


def setup_case(store, case):
    run = "cbr-" + uuid.uuid4().hex
    world = run + "-cb"
    rels = default_relationships()
    if case == "CB-R03":
        rels = [r.model_copy(update={"funding_minor": 100}) if r.id == "AS_USD" else r for r in rels]
    if case == "CB-R04":
        rels = [
            r.model_copy(update={"funding_minor": 100}) if r.id in {"SR_USD", "RM_USD"} else r for r in rels
        ]
    if case == "CB-R09":
        rels = [r.model_copy(update={"funding_minor": 15000}) if r.id == "AS_USD" else r for r in rels]
    if case == "CB-R05":
        rels = [r.model_copy(update={"variable_fee_bps": 7}) if r.id == "SR_SGD" else r for r in rels]
    CorrespondentRail(store).initialize(world, rels)
    router = Router(store)
    router.initialize(run)
    intent = Intent(
        payment_id="p",
        idempotency_key="p",
        destination_currency=Currency.SGD if case == "CB-R05" else Currency.USD,
        maximum_fees_by_currency={Currency.USD: 1000, Currency.SGD: 1000},
    )
    network = Network(worlds={"CORRESPONDENT": world})
    record = router.evaluate(
        run, intent, network, 0, Policy(preference=("CORRESPONDENT", "INSTANT_LINK", "ATOMIC_PVP"))
    )
    return router, run, world, intent, network, record


def checks(store, run, world):
    for w in (run, world):
        r = reconcile(store, w)
        assert r["financial_errors"] == 0, r
        assert replay_check(store, w)["match"]


def finish(router, run, start=100, end=1500):
    for t in range(start, end + 1, 100):
        outcome = router.recover(run, "p", t)
        if (
            outcome["payment"]["economic_status"] == "SETTLED"
            and outcome["payment"]["ack_status"] == "ACKNOWLEDGED"
        ):
            break
    result = router.status(run, "p")
    assert result["payment"]["economic_status"] == "SETTLED", result
    assert result["chain"]["end_to_end_completed"]
    return result


@pytest.mark.parametrize("case", CASES)
def test_correspondent_routing_matrix(store, case):
    if case == "CB-R12":
        from railstorm.routing_experiments import experiment

        seed = experiment(store, "RT-05")
        e = seed["record"]["evaluation"]
        run = seed["control_run"]
        router = Router(store)
        network = Network.model_validate(e["network"]).model_copy(update={"quote_available": True})
        intent = Intent(
            payment_id="p",
            idempotency_key="p",
            maximum_fees_by_currency={Currency.USD: 1000, Currency.SGD: 1000},
        )
        record = router.evaluate(
            run, intent, network, 0, Policy(preference=("CORRESPONDENT", "ATOMIC_PVP", "INSTANT_LINK"))
        )
        assert all(c["status"] == "FEASIBLE" for c in record["evaluation"]["candidates"])
        assert record["evaluation"]["selected"] == "CORRESPONDENT"
        router.execute(run, "p", 0)
        result = finish(router, run)
        checks(store, run, network.worlds["CORRESPONDENT"])
        # Equivalent isolated source/provider claims and intent; other rail policies invoke their own engines.
        for selected in ("ATOMIC_PVP", "INSTANT_LINK"):
            other = experiment(store, "RT-05")
            other_run = other["control_run"]
            other_network = Network.model_validate(other["record"]["evaluation"]["network"]).model_copy(
                update={"quote_available": True}
            )
            router.evaluate(other_run, intent, other_network, 0, Policy(preference=(selected,)))
            output = router.execute(other_run, "p", 0)
            assert output["bound_rail"] == selected
            if selected == "INSTANT_LINK":
                router.recover(other_run, "p", 30)
            assert router.status(other_run, "p")["payment"]["economic_status"] == "SETTLED"
            checks(store, other_run, other_network.worlds[selected])
        return
    router, run, world, intent, network, record = setup_case(store, case)
    candidate = next(c for c in record["evaluation"]["candidates"] if c["rail"] == "CORRESPONDENT")
    if case in ("CB-R03", "CB-R04"):
        assert candidate["status"] == "INSUFFICIENTLY_FUNDED"
        shortage = next(r for r in candidate["reasons"] if r["code"] == "LIQUIDITY_SHORTAGE")
        assert shortage["account"] == ("VOSTRO:AS_USD" if case == "CB-R03" else "VOSTRO:SR_USD")
        assert shortage["shortfall_minor"] == (9900 if case == "CB-R03" else 9800)
        with pytest.raises(DomainError):
            router.execute(run, "p", 0)
        with store.transaction(world) as tx:
            assert not tx.rows("SELECT * FROM reservations WHERE run_id=:r")
            assert not tx.rows("SELECT * FROM payments WHERE run_id=:r")
        checks(store, run, world)
        return
    assert candidate["status"] == "FEASIBLE", candidate
    router.execute(run, "p", 0)
    if case in ("CB-R06", "CB-R07", "CB-R10", "CB-R11"):
        router.recover(run, "p", 100)
        router.recover(run, "p", 150)
        fault = Fault(id="down", type="PARTICIPANT_DOWN", target="RHINE_EU", at_ms=151, duration_ms=649)
        apply_fault(store, world, fault, True, 151)
        router.recover(run, "p", 200)
        pending = router.status(run, "p")
        assert pending["payment"]["economic_status"] == "PENDING_EXPOSURE"
        assert pending["chain"]["pending_exposure_minor"] == {"USD": 9900}
        assert pending["chain"]["hops"][0]["posting_status"] == "POSTED"
        assert not pending["chain"]["beneficiary_credited"]
        checks(store, run, world)
        if case == "CB-R10":
            # Same identity cannot be replaced with a different permitted rail set after binding.
            changed = intent.model_copy(update={"permitted_rails": ("ATOMIC_PVP",)})
            with pytest.raises(DomainError, match="INTENT_ID_REUSED"):
                router.evaluate(run, changed, network, 200)
            assert router.read(run, "p")["bound_rail"] == "CORRESPONDENT"
        apply_fault(store, world, fault, False, 800)
        result = finish(router, run, 900, 2000)
    elif case == "CB-R09":
        other = intent.model_copy(update={"payment_id": "other", "idempotency_key": "other"})
        router.evaluate(run, other, network, 0, Policy(preference=("CORRESPONDENT",)))
        router.execute(run, "other", 0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda p: router.recover(run, p, 100), ["p", "other"]))
        with store.transaction(world) as tx:
            assert tx.position("VOSTRO:AS_USD")["posted"] == 5000
            assert tx.position("NOSTRO:AS_USD")["posted"] == -5000
            hops = tx.rows(
                "SELECT data FROM cb_hops WHERE run_id=:r AND hop_index=0 AND data->>'posting_status'='POSTED'"
            )
            assert len(hops) == 1
            assert not tx.rows("SELECT * FROM reservations WHERE run_id=:r AND status='ACTIVE'")
        checks(store, run, world)
        return
    else:
        result = finish(router, run)
    if case != "CB-R05":
        assert result["payment"]["beneficiary_minor"] == 9825
        assert result["realized_fees_by_currency"] == {"USD": 175}
        assert [h["receiver"] for h in result["chain"]["hops"]] == ["STERLING_UK", "RHINE_EU", "MERIDIAN_SG"]
        with store.transaction(world) as tx:
            for account, expected in {
                "VOSTRO:AS_USD": 990000,
                "NOSTRO:AS_USD": -990000,
                "VOSTRO:SR_USD": 990100,
                "NOSTRO:SR_USD": -990100,
                "VOSTRO:RM_USD": 990150,
                "NOSTRO:RM_USD": -990150,
                "BENEFICIARY:p:USD": 9825,
                "GL:STERLING_UK:FEES:USD": 100,
                "GL:RHINE_EU:FEES:USD": 50,
                "GL:MERIDIAN_SG:FEES:USD": 25,
            }.items():
                assert tx.position(account)["posted"] == expected
    else:
        # USD100 source fee; floor(9900*133/100)=13167; SGD fee50+floor(13167*7/10000)=59; finalSGD25.
        assert result["payment"]["beneficiary_minor"] == 13083
        assert result["realized_fees_by_currency"] == {"SGD": 84, "USD": 100}
        assert candidate["fees_by_currency"] == result["realized_fees_by_currency"]
    if case == "CB-R02":
        direct = world + "-direct"
        rail = CorrespondentRail(store)
        rail.initialize(direct)
        rail.create(direct, ChainCommand(idempotency_key="p"))
        for t in range(100, 1600, 100):
            rail.advance(direct, "p", t)
        with store.transaction(direct) as tx:
            assert tx.position("BENEFICIARY:p:USD")["posted"] == 9825
        assert reconcile(store, direct)["financial_errors"] == 0
    if case in ("CB-R07", "CB-R08"):
        for _ in range(3):
            router.execute(run, "p", 2100)
        with store.transaction(world) as tx:
            assert (
                tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id='p'")["n"] == 6
            )
    checks(store, run, world)


def test_correspondent_routing_api_pending_restore_and_refresh(store):
    from fastapi.testclient import TestClient

    from railstorm.api import create_app

    with TestClient(create_app(store)) as client:
        result = client.post("/api/v1/routing/correspondent/CB-R06").json()
        run = result["control_run"]
        base = f"/api/v1/routing/{run}/payment"
        assert result["status"]["payment"]["economic_status"] == "PENDING_EXPOSURE"
        assert result["status"]["chain"]["pending_exposure_minor"] == {"USD": 9900}
    with TestClient(create_app(store)) as client:
        assert client.get(base + "/status").json()["chain"]["hops"][0]["posting_status"] == "POSTED"
        assert client.post(base + "/restore", json={"at_ms": 800}).status_code == 200
        for t in range(900, 1600, 100):
            assert client.post(base + "/recover", json={"at_ms": t}).status_code == 200
        status = client.get(base + "/status").json()
        assert status["chain"]["end_to_end_completed"]
        assert status["realized_fees_by_currency"] == {"USD": 175}
        assert client.get(base + "/verification").json()["settlement"]["financial_errors"] == 0


def test_unsafe_fallback_rejected_with_real_alternative_worlds(store):
    from railstorm.correspondent_routing_examples import example

    result = example(store, "CB-R06")
    router = Router(store)
    run = result["control_run"]
    e = result["record"]["evaluation"]
    intent = Intent.model_validate(e["intent"])
    network = Network.model_validate(e["network"])
    record = router.evaluate(run, intent, network, 200, Policy(preference=("ATOMIC_PVP", "INSTANT_LINK")))
    assert record["bound_rail"] == "CORRESPONDENT"
    router.execute(run, "payment", 200)
    for rail in ("ATOMIC_PVP", "INSTANT_LINK"):
        with store.transaction(network.worlds[rail]) as tx:
            assert not tx.rows("SELECT * FROM payments WHERE run_id=:r")
            assert not tx.rows("SELECT * FROM journal_entries WHERE run_id=:r AND payment_id IS NOT NULL")
    checks(store, run, network.worlds["CORRESPONDENT"])


def test_currency_budgets_are_not_silently_added_or_overridden(store):
    router, run, world, intent, network, _ = setup_case(store, "CB-R01")
    lower = intent.model_copy(
        update={
            "payment_id": "budget",
            "idempotency_key": "budget",
            "maximum_fee_minor": 100,
            "maximum_fees_by_currency": {Currency.USD: 1000},
        }
    )
    result = router.evaluate(run, lower, network)
    c = next(c for c in result["evaluation"]["candidates"] if c["rail"] == "CORRESPONDENT")
    assert c["status"] == "STRUCTURALLY_INELIGIBLE"
    assert any(r["code"] == "FEE_LIMIT" for r in c["reasons"])


def test_fee_valuation_limitation_and_known_cutoff_are_explicit(store):
    router, run, world, intent, network, _ = setup_case(store, "CB-R05")
    unvalued = intent.model_copy(
        update={"payment_id": "noquote", "idempotency_key": "noquote", "maximum_fees_by_currency": None}
    )
    record = router.evaluate(run, unvalued, network)
    candidate = next(c for c in record["evaluation"]["candidates"] if c["rail"] == "CORRESPONDENT")
    assert candidate["status"] == "QUOTE_UNAVAILABLE"
    assert any(
        r["code"] == "FEE_REPORTING_CURRENCY_UNAVAILABLE" and r["currency"] == "SGD"
        for r in candidate["reasons"]
    )
    # A separately initialized model has an early cutoff; do not mutate immutable relationship config.
    rels = [
        r.model_copy(update={"cutoff_ms": 50}) if r.id == "AS_USD" else r for r in default_relationships()
    ]
    closed = world + "-closed"
    CorrespondentRail(store).initialize(closed, rels)
    eval = router.enumerate(
        Intent(payment_id="closed", idempotency_key="closed", destination_currency=Currency.USD),
        Network(worlds={"CORRESPONDENT": closed}),
        0,
        Policy(),
    )
    c = next(c for c in eval["candidates"] if c["rail"] == "CORRESPONDENT")
    assert c["status"] == "TEMPORARILY_UNAVAILABLE"
    assert any(r["code"] == "OPERATING_WINDOW_BEFORE_STAGE" for r in c["reasons"])


def test_optional_currency_budget_preserves_legacy_intent_fingerprint(store):
    from railstorm.domain import digest

    router, run, world, intent, network, record = setup_case(store, "CB-R01")
    legacy = Intent(payment_id="legacy", idempotency_key="legacy", destination_currency=Currency.USD)
    data = legacy.model_dump(mode="json")
    data.pop("maximum_fees_by_currency")
    result = router.evaluate(run, legacy, network)
    assert result["fingerprint"] == digest(data)
    assert router.evaluate(run, Intent.model_validate(data), network) == result


def test_unsupported_instant_corridor_does_not_advertise_invented_output(store):
    router, run, world, intent, network, record = setup_case(store, "CB-R01")
    from railstorm.rails.instant_link import InstantLink

    il = world + "-il"
    InstantLink(store).initialize(il)
    result = router.enumerate(
        intent, Network(worlds={"CORRESPONDENT": world, "INSTANT_LINK": il}), 0, Policy()
    )
    candidate = next(c for c in result["candidates"] if c["rail"] == "INSTANT_LINK")
    assert candidate["status"] == "STRUCTURALLY_INELIGIBLE"
    assert candidate["beneficiary_minor"] is None
    assert candidate["fees_by_currency"] == {}
    assert candidate["liquidity"] == []
