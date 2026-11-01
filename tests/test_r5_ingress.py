from fastapi.testclient import TestClient

from railstorm.api import create_app


def test_compose_gateway_requires_explicit_trust_and_loopback_host(store, monkeypatch):
    import railstorm.live_crisis_api as operators

    monkeypatch.delenv("RAILSTORM_TRUST_LOCAL_COMPOSE_GATEWAY", raising=False)
    monkeypatch.setattr(operators, "bridge_gateway", lambda: "172.19.0.1")
    app = create_app(store)

    def from_client(host):
        async def application(scope, receive, send):
            if scope["type"] == "http":
                scope = {**scope, "client": (host, 1234)}
            await app(scope, receive, send)

        return application

    with TestClient(from_client("172.19.0.1"), base_url="http://127.0.0.1:8840") as client:
        assert client.post("/api/v1/live-crisis/sessions", json={"scenario": "instant"}).status_code == 409
        monkeypatch.setenv("RAILSTORM_TRUST_LOCAL_COMPOSE_GATEWAY", "1")
        accepted = client.post(
            "/api/v1/live-crisis/sessions",
            json={"scenario": "instant"},
            headers={"Origin": "http://127.0.0.1:8840"},
        )
        assert accepted.status_code == 200
        assert (
            client.post(
                "/api/v1/live-crisis/sessions",
                json={"scenario": "instant"},
                headers={"Origin": "https://example.com"},
            ).status_code
            == 409
        )
    with TestClient(from_client("172.19.0.9"), base_url="http://127.0.0.1:8840") as client:
        assert client.post("/api/v1/live-crisis/sessions", json={"scenario": "instant"}).status_code == 409
    with TestClient(from_client("172.19.0.1"), base_url="http://example.com") as client:
        assert client.post("/api/v1/live-crisis/sessions", json={"scenario": "instant"}).status_code == 409
