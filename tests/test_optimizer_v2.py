from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from railstorm.api import create_app
from railstorm.optimizer import choose
from railstorm.optimizer_experiments import compare, run_policy, specification
from railstorm.optimizer_v2 import diagnostic_mode
from railstorm.routing import Policy
from tests.test_optimizer import candidate


def observed(rail, fee=25, proceeds=13300, exposure=0, pressure=10):
    c = candidate(rail, pressure, 100, fee)
    c.update(
        beneficiary_minor=proceeds,
        exposure_observation={
            "open_by_currency": {},
            "potential_peak_by_currency": {"SGD": exposure},
            "nominal_hold_minor_ms_by_currency": {"USD": 1000},
        },
    )
    return c


def test_v2_cost_dominance_prevents_v1_large_account_bias():
    a = observed("INSTANT_LINK", exposure=13300, pressure=50)
    b = observed("CORRESPONDENT", fee=100, proceeds=13100, pressure=1)
    assert choose([a, b], Policy(version="LIQUIDITY_AWARE_V1"))[0] == "CORRESPONDENT"
    selected, scores = choose([a, b], Policy(version="LIQUIDITY_AWARE_V2"))
    assert selected == "INSTANT_LINK"
    assert scores[0]["excluded_cost_dominated"] == ["CORRESPONDENT"]


def test_v2_no_cross_currency_nominal_dominance():
    a = observed("ATOMIC_PVP", fee=20)
    a["fees_by_currency"]["SGD"] = 100
    b = observed("INSTANT_LINK", fee=25)
    _, scores = choose([a, b], Policy(version="LIQUIDITY_AWARE_V2"))
    assert set(scores[0]["cost_frontier"]) == {"ATOMIC_PVP", "INSTANT_LINK"}


def test_v2_exposure_tie_and_hard_eligibility():
    a, b = observed("ATOMIC_PVP"), observed("INSTANT_LINK", exposure=13300)
    assert choose([b, a], Policy(version="LIQUIDITY_AWARE_V2"))[0] == "ATOMIC_PVP"
    a["status"] = "INSUFFICIENTLY_FUNDED"
    assert choose([a, b], Policy(version="LIQUIDITY_AWARE_V2"))[0] == "INSTANT_LINK"


def test_v2_future_fields_do_not_affect_choice():
    candidates = [observed("ATOMIC_PVP"), observed("INSTANT_LINK", exposure=13300)]
    before = choose(candidates, Policy(version="LIQUIDITY_AWARE_V2"))
    changed = deepcopy(candidates)
    for c in changed:
        c["future_outages"] = ["arbitrary hidden future"]
    assert before == choose(changed, Policy(version="LIQUIDITY_AWARE_V2"))


def test_diagnostic_cost_ablation_scoped_and_disclosed():
    a, b = observed("ATOMIC_PVP"), observed("INSTANT_LINK", exposure=13300)
    a["latency_ms"] = 100
    token = diagnostic_mode.set("cost_only")
    try:
        selected, scores = choose([a, b], Policy(version="LIQUIDITY_AWARE_V2"))
        assert selected == "INSTANT_LINK"
        assert scores[0]["diagnostic_mode"] == "cost_only"
    finally:
        diagnostic_mode.reset(token)
    assert choose([a, b], Policy(version="LIQUIDITY_AWARE_V2"))[0] == "ATOMIC_PVP"


def test_three_policy_equal_states_and_v2_replay(store):
    comparison = compare(store, "OPT-01", 11, 1, persist=False, include_v2=True)
    assert len(comparison["policies"]) == 3
    assert len({r["initial_state_hash"] for r in comparison["policies"]}) == 1
    result = comparison["policies"][2]
    assert result["decisions"][0]["selected"] == "ATOMIC_PVP"
    assert result["metrics"]["completed"] == 1
    assert result["routing_audit"]["replay"]["match"]
    rerun = run_policy(store, specification("OPT-01", 11, 1), "LIQUIDITY_AWARE_V2")
    assert result["canonical_result_hash"] == rerun["canonical_result_hash"]


def test_v2_pending_not_completed_and_native_finality(store):
    result = run_policy(store, specification("OPT-09", 11, 1), "LIQUIDITY_AWARE_V2")
    assert result["metrics"]["completed"] == 0
    assert result["metrics"]["pending"] == 1
    assert result["metrics"]["duplicate_financial_postings"] == 0
    assert result["metrics"]["reconciliation_errors"] == 0
    assert not result["metrics"]["pending_obligations"]


def test_api_opt_in_v2_and_default_stays_fixed(store):
    client = TestClient(create_app(store))
    response = client.post(
        "/api/v1/optimizer/comparisons",
        json={"scenario": "OPT-01", "seed": 11, "count": 1, "include_v2": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["policies"]) == 3
    assert client.get("/api/v1/optimizer/comparisons/" + data["id"]).json() == data
    assert Policy().version == "fixed-order-v1"


@pytest.mark.parametrize("count", [0, 101])
def test_experiment_count_bound(count):
    with pytest.raises(ValueError):
        specification("OPT-01", 131, count)


def test_unfunded_first_correspondent_hop_has_bounded_retries(store):
    result = run_policy(store, specification("OPT-04", 11, 13), "fixed-order-v1")
    assert result["metrics"]["submitted"] == 13
    assert result["metrics"]["completed"] == 11
    assert result["metrics"]["pending"] == 1
    assert result["metrics"]["progress_attempts"] < 100
    pending = [
        v["payment"]
        for v in result["outcomes"].values()
        if v["payment"] and v["payment"]["economic_status"] == "QUEUED"
    ]
    assert len(pending) == 1 and pending[0]["next_stage"] == 0
    assert result["metrics"]["reconciliation_errors"] == 0
