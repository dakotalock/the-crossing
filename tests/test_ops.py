from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crossing import abuse, crypto, providers
from crossing.api import app
from crossing.policy import PolicyDenied, Reason
from crossing.providers import ProviderConfigError
from crossing.receipts import verify_receipt
from crossing.sdk import CrossingClient


def test_provider_ssrf_blocked():
    with pytest.raises(ProviderConfigError):
        providers.assert_public_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(ProviderConfigError):
        providers.assert_public_url("http://127.0.0.1/admin")
    with pytest.raises(ProviderConfigError):
        providers.assert_public_url("http://localhost/x")
    with pytest.raises(ProviderConfigError):
        providers.assert_public_url("http://10.0.0.8/secret")
    with pytest.raises(ProviderConfigError):
        providers.assert_public_url("file:///etc/passwd")
    with pytest.raises(PolicyDenied) as exc:
        providers.get_provider("https://evil.example/mcp")
    assert exc.value.reason == Reason.SERVER_NOT_ALLOWED
    with pytest.raises(PolicyDenied):
        providers.get_provider("shell")


def test_allowlisted_loopback_still_blocked(monkeypatch):
    monkeypatch.setenv("CROSSING_PROVIDER_URLS", "evil=http://127.0.0.1:9/rpc")
    monkeypatch.delenv("CROSSING_PROVIDER_INTERNAL_ALLOWLIST", raising=False)
    with pytest.raises(ProviderConfigError):
        providers.get_provider("evil")


def test_healthz_and_readyz(cx):
    client = TestClient(app)
    hz = client.get("/healthz")
    assert hz.status_code == 200
    assert hz.json()["status"] == "ok"
    rz = client.get("/readyz")
    assert rz.status_code == 200
    assert rz.json()["status"] == "ready"
    mx = client.get("/metrics")
    assert mx.status_code == 200
    assert "crossing_invokes_total" in mx.text
    js = client.get("/metrics?format=json")
    assert "invokes_total" in js.json()


def test_receipt_has_kid(seeded):
    cx, _, _, m = seeded
    r = cx.invoke(m.id, "search", {"q": "kid"}, idempotency_key="kid-1")
    assert r.ok
    body = r.receipt["body"]
    assert body.get("kid")
    assert r.receipt.get("kid") == body["kid"]
    assert body["kid"] == crypto.key_id()
    assert "result" not in body
    assert verify_receipt(r.receipt)


def test_sdk_smoke(seeded):
    cx, p, a, m = seeded
    acct = cx.account(p.id)
    assert acct["account_id"] == p.account_id
    r = cx.invoke(m.id, "search", {"q": "sdk"}, idempotency_key="sdk-1")
    assert r.ok
    got = cx.get_receipt(r.receipt["id"])
    assert got and cx.verify_receipt(got)
    assert cx.remaining_budget(m.id) == 95
    status = cx.billing_status(p.account_id)
    assert status["account_id"] == p.account_id
    client = TestClient(app)
    client.headers["X-API-Key"] = "dev"
    acc = client.get("/v1/account")
    assert acc.status_code == 200
    assert acc.json()["account_id"]
    http = CrossingClient("http://example.invalid", "dev")
    assert hasattr(http, "invoke")
    http.close()


def test_rate_limit_invoke(cx, monkeypatch):
    monkeypatch.setenv("CROSSING_RATE_LIMIT_PER_MINUTE", "2")
    abuse.reset_for_tests()
    client = TestClient(app)
    client.headers["X-API-Key"] = "dev"
    p = client.post("/v1/principals", json={"name": "Rl"}).json()
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
    ok1 = client.post(
        "/v1/invoke",
        json={"mandate_id": m["id"], "tool": "search", "arguments": {"q": "a"}, "idempotency_key": "rl-1"},
    )
    ok2 = client.post(
        "/v1/invoke",
        json={"mandate_id": m["id"], "tool": "search", "arguments": {"q": "b"}, "idempotency_key": "rl-2"},
    )
    denied = client.post(
        "/v1/invoke",
        json={"mandate_id": m["id"], "tool": "search", "arguments": {"q": "c"}, "idempotency_key": "rl-3"},
    )
    assert ok1.status_code == 200
    assert ok2.status_code == 200
    assert denied.status_code == 429
    assert denied.json()["detail"]["reason"] == Reason.RATE_LIMITED
