from __future__ import annotations

from fastapi.testclient import TestClient

from crossing.api import app


def test_health_and_story(cx):
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    p = client.post("/v1/principals", json={"name": "Bob"}).json()
    a = client.post("/v1/agents", json={"principal_id": p["id"], "name": "bot"}).json()
    m = client.post(
        "/v1/mandates",
        json={
            "principal_id": p["id"],
            "agent_id": a["id"],
            "spend_limit_cents": 100,
            "tools": ["search"],
            "servers": ["mock"],
        },
    ).json()
    inv = client.post(
        "/v1/invoke",
        json={"mandate_id": m["id"], "tool": "search", "arguments": {"q": "api"}, "idempotency_key": "api-1"},
    )
    assert inv.status_code == 200
    denied = client.post("/v1/invoke", json={"mandate_id": m["id"], "tool": "purchase", "arguments": {}})
    assert denied.status_code == 403
    recs = client.get("/v1/receipts").json()
    assert recs
    one = client.get(f"/v1/receipts/{recs[0]['id']}").json()
    assert one["valid"] is True
    html = client.get("/").text
    assert "principals" in html
    assert "ledger_events" in html
