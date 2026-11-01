import pytest
from fastapi.testclient import TestClient

from railstorm.api import create_app
from railstorm.live_crisis import LiveCrisis


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("atomic", "SETTLED"),
        ("multicurrency", "SETTLED"),
        ("correspondent", "SETTLED"),
        ("instant", "SETTLED"),
        ("expired", "EXPIRED"),
        ("shortage", "REJECTED"),
    ],
)
def test_live_native_scenarios(store, scenario, expected):
    with TestClient(create_app(store)) as client:
        result = client.post("/api/v1/live-crisis/sessions", json={"scenario": scenario, "seed": 17})
        assert result.status_code == 200, result.text
        run = result.json()["session"]["id"]
        path = f"/api/v1/live-crisis/sessions/{run}/actions"
        for _ in range(20):
            result = client.post(path, json={"action": "ADVANCE"})
            assert result.status_code == 200, result.text
            payment = result.json()["financial"]["payments"]["payment"]
            if payment["economic_status"] in ("SETTLED", "EXPIRED", "REJECTED"):
                break
        assert payment["economic_status"] == expected
        audit = client.post(path, json={"action": "AUDIT"}).json()["session"]["audit"]
        assert audit["native"]["financial_errors"] == 0 and audit["independent"]["pass"]
        assert client.post(path, json={"action": "REPLAY"}).json()["session"]["replay"]["match"]
        assert LiveCrisis(store).inspect(run)["session"]["stage"] == result.json()["session"]["stage"]


def test_live_outage_restoration_and_illegal_transition(store):
    with TestClient(create_app(store)) as client:
        run = client.post("/api/v1/live-crisis/sessions", json={"scenario": "instant"}).json()["session"][
            "id"
        ]
        path = f"/api/v1/live-crisis/sessions/{run}/actions"
        assert client.post(path, json={"action": "RESTORE"}).status_code == 409
        for action in ("ADVANCE", "OUTAGE", "ADVANCE", "ADVANCE"):
            r = client.post(path, json={"action": action})
            assert r.status_code == 200, r.text
        p = r.json()["financial"]["payments"]["payment"]
        assert p["source_final"] and not p["destination_final"] and p["obligation"]["amount_minor"] == 13300
        for action in ("RESTORE", "RECOVER", "RETRY", "AUDIT", "REPLAY"):
            r = client.post(path, json={"action": action})
            assert r.status_code == 200, r.text
        data = r.json()
        assert data["financial"]["positions"]["MERIDIAN_SG:SGD"]["posted"] == 13300
        assert data["session"]["audit"]["independent"]["pass"] and data["session"]["replay"]["match"]
        assert client.post(path, json={"action": "ADVANCE"}).status_code == 409


def test_live_operator_origin_guard(store):
    with TestClient(create_app(store)) as client:
        assert (
            client.post(
                "/api/v1/live-crisis/sessions",
                json={"scenario": "instant"},
                headers={"Origin": "https://example.com"},
            ).status_code
            == 409
        )
