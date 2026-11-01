import uuid
from copy import deepcopy

from railstorm.audit import reconcile
from railstorm.optimizer import choose
from railstorm.optimizer_experiments import compare, run_policy, specification
from railstorm.rails.instant_link import InstantLink
from railstorm.routing import Intent, Network, Policy, Router


def candidate(rail, required, available, fee=25, status="FEASIBLE"):
    return {
        "rail": rail,
        "route_id": rail + "-v1",
        "status": status,
        "liquidity": [
            {
                "account": "named-account",
                "currency": "USD",
                "required_minor": required,
                "available_minor": available,
            }
        ],
        "currencies": ["USD", "SGD"],
        "fees_by_currency": {"USD": fee},
        "latency_ms": 30,
    }


def test_policy_exact_local_shares_and_baseline_unchanged():
    candidates = [
        candidate("ATOMIC_PVP", 6, 10),
        candidate("INSTANT_LINK", 5, 10, fee=100),
        candidate("CORRESPONDENT", 1, 100, status="INSUFFICIENTLY_FUNDED"),
    ]
    baseline = Policy(preference=("ATOMIC_PVP", "INSTANT_LINK", "CORRESPONDENT"))
    assert choose(candidates, baseline)[0] == "ATOMIC_PVP"
    optimizer = Policy(version="LIQUIDITY_AWARE_V1", preference=baseline.preference)
    selected, scores = choose(candidates, optimizer)
    assert selected == "INSTANT_LINK"
    assert scores[0]["max_local_funding_share"] == {"numerator": 1, "denominator": 2}
    assert len(scores) == 2
    before = choose(candidates, optimizer)
    changed = deepcopy(candidates)
    for c in changed:
        c["future_outage"] = "arbitrary hidden data never read"
    assert choose(changed, optimizer) == before


def test_currency_fee_coordinates_are_not_nominally_summed():
    a = candidate("ATOMIC_PVP", 1, 2, fee=20)
    a["fees_by_currency"]["SGD"] = 100
    b = candidate("INSTANT_LINK", 2, 4, fee=25)
    selected, scores = choose([a, b], Policy(version="LIQUIDITY_AWARE_V1"))
    assert selected == "ATOMIC_PVP"  # lexicographic USD20 before USD25; no USD+SGD sum.
    assert scores[0]["fee_currency_order"] == ["USD", "SGD"]
    assert scores[0]["fee_vector_minor"] == [20, 100]


def test_prepare_only_does_not_advance_instant_time(store):
    run = "opt-prep-" + uuid.uuid4().hex
    world = run + "-il"
    InstantLink(store).initialize(world)
    router = Router(store)
    router.initialize(run)
    router.evaluate(run, Intent(payment_id="p", idempotency_key="p"), Network(worlds={"INSTANT_LINK": world}))
    router.execute(run, "p", 0, prepare_only=True)
    with store.transaction(world) as tx:
        assert tx.run["logical_time_ms"] == 0
        assert not tx.payment("p")["source_final"]
        assert not tx.rows("SELECT * FROM journal_entries WHERE run_id=:r AND payment_id='p'")
    assert reconcile(store, world)["financial_errors"] == 0


def test_paired_initial_conditions_and_canonical_reproduction(store):
    pair = compare(store, "OPT-01", seed=11, count=1, persist=False)
    a, b = pair["policies"]
    assert pair["fairness_checked"]
    assert a["physical_runs"] != b["physical_runs"]
    for key in (
        "input_hash",
        "initial_state_hash",
        "configuration_hash",
        "workload_hash",
        "fault_schedule_hash",
    ):
        assert a[key] == b[key]
    repeat = run_policy(store, pair["specification"], "fixed-order-v1")
    assert repeat["canonical_result_hash"] == a["canonical_result_hash"]
    assert repeat["metrics"] == a["metrics"]
    assert a["metrics"]["completed"] == b["metrics"]["completed"] == 1


def test_fault_before_source_finality_is_not_skipped(store):
    spec = specification("OPT-09", 11, 1)
    spec["faults"] = [
        {"kind": "PARTICIPANT_DOWN", "id": "source-window", "bank": "ATLAS_US", "start": 5, "end": 40}
    ]
    for policy in ("fixed-order-v1", "LIQUIDITY_AWARE_V1"):
        result = run_policy(store, spec, policy)
        p = result["outcomes"]["p0000"]["payment"]
        assert p["economic_status"] == "SETTLED"
        if result["outcomes"]["p0000"]["receipt"]["selected"] == "INSTANT_LINK":
            assert p["source_final_ms"] >= 40
        assert result["metrics"]["reconciliation_errors"] == 0


def test_horizon_pending_obligation_is_not_completed(store):
    result = run_policy(store, specification("OPT-09", 11, 1), "fixed-order-v1")
    m = result["metrics"]
    assert m["submitted"] == 1 and m["completed"] == 0 and m["pending"] == 1
    assert m["success_rate"] == {"numerator": 0, "denominator": 1}
    assert m["fees_by_currency"] == {"USD": 25}
    assert m["pending_obligations"][0]["currency"] == "SGD"
    assert m["peak_reserved_by_currency"]["SGD"] > 0
    assert m["obligation_amount_time_minor_ms"]["SGD"] > 0


def test_adversarial_retry_and_freeze_preserve_financial_effects(store):
    result = run_policy(store, specification("OPT-10", 11, 1), "fixed-order-v1")
    assert result["metrics"]["completed"] == 1
    assert result["metrics"]["duplicate_financial_postings"] == 0
    assert result["metrics"]["fees_by_currency"] == {"USD": 25}
    assert result["metrics"]["recovery_attempts"] >= 1
    assert result["freeze_observations"]
    assert all(
        a["replay"]["match"] and a["financial_errors"] == 0 for a in result["financial_audits"].values()
    )


def test_optimizer_api_uses_persisted_artifact_and_validates_input(store):
    from fastapi.testclient import TestClient

    from railstorm.api import create_app

    with TestClient(create_app(store)) as client:
        result = client.post(
            "/api/v1/optimizer/comparisons", json={"scenario": "OPT-01", "seed": 11, "count": 1}
        )
        assert result.status_code == 200
        data = result.json()
        saved = client.get("/api/v1/optimizer/comparisons/" + data["id"])
        assert saved.json() == data
        assert data["fairness_checked"]
        assert (
            client.post(
                "/api/v1/optimizer/comparisons", json={"scenario": "OPT-01", "count": 500}
            ).status_code
            == 422
        )
        assert client.get("/api/v1/optimizer/comparisons/not-a-valid-id").status_code == 404


def test_fairness_guard_refuses_unequal_initial_states(store, monkeypatch):
    import railstorm.optimizer_experiments as experiments

    def invalid_run(instance, spec, version):
        return {
            "input_hash": "same",
            "workload_hash": "same",
            "configuration_hash": "same",
            "fault_schedule_hash": "same",
            "initial_state_hash": version,
        }

    monkeypatch.setattr(experiments, "run_policy", invalid_run)
    import pytest

    with pytest.raises(AssertionError, match="initial_state_hash"):
        experiments.compare(store, "OPT-01", 11, 1, persist=False)
