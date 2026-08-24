from __future__ import annotations

from fastapi.testclient import TestClient

from crossing import auth, db
from crossing.api import app
from crossing.models import ApiKey, Principal


def _customer(name: str):
    with db.session_scope() as s:
        p = __import__("crossing.identity", fromlist=["create_principal"]).create_principal(s, name)
        a = __import__("crossing.identity", fromlist=["create_agent"]).create_agent(s, p.id, "bot")
        issued = auth.issue_api_key(s, account_id=p.account_id, kind="customer")
        return p.id, a.id, p.account_id, issued.secret, issued.record.id, issued.record.prefix


def test_secret_hash_not_raw_stored(cx):
    _pid, _aid, _acct, secret, key_id, prefix = _customer("HashCheck")
    assert secret.startswith(prefix)
    with db.session_scope() as s:
        row = s.get(ApiKey, key_id)
        assert row is not None
        assert row.secret_hash != secret
        assert secret not in row.secret_hash
        assert len(row.secret_hash) == 64


def test_revoked_key_401(cx):
    _pid, _aid, _acct, secret, key_id, _prefix = _customer("Revoked")
    client = TestClient(app)
    ok = client.get("/v1/principals", headers={"X-API-Key": secret})
    assert ok.status_code == 200
    with db.session_scope() as s:
        auth.revoke_api_key(s, key_id)
    denied = client.get("/v1/principals", headers={"X-API-Key": secret})
    assert denied.status_code == 401


def test_cors_default_deny(cx):
    client = TestClient(app)
    r = client.options(
        "/v1/principals",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.headers.get("access-control-allow-origin") in (None, "")
    assert r.headers.get("access-control-allow-origin") != "*"


def test_hostile_cross_tenant_read_write_mandate_receipt_invoke(cx):
    from datetime import datetime, timedelta, timezone

    from crossing.mandate import issue_mandate

    p1, a1, acct1, sec1, _k1, _pr1 = _customer("TenantA")
    p2, a2, acct2, sec2, _k2, _pr2 = _customer("TenantB")
    with db.session_scope() as s:
        m1 = issue_mandate(
            s,
            principal_id=p1,
            agent_id=a1,
            spend_limit_cents=100,
            tools=["search"],
            servers=["mock"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        m1_id = m1.id
    client = TestClient(app)
    ha, hb = {"X-API-Key": sec1}, {"X-API-Key": sec2}

    listed = client.get("/v1/principals", headers=hb).json()
    assert all(row["id"] != p1 for row in listed)
    assert any(row["id"] == p2 for row in listed)

    assert client.get(f"/v1/mandates/{m1_id}", headers=hb).status_code == 404
    assert client.get(f"/v1/mandates/{m1_id}", headers=ha).status_code == 200

    steal_agent = client.post(
        "/v1/agents",
        json={"principal_id": p1, "name": "intruder"},
        headers=hb,
    )
    assert steal_agent.status_code == 404

    steal_mandate = client.post(
        "/v1/mandates",
        json={
            "principal_id": p1,
            "agent_id": a1,
            "spend_limit_cents": 10,
            "tools": ["search"],
            "servers": ["mock"],
        },
        headers=hb,
    )
    assert steal_mandate.status_code == 404

    steal_invoke = client.post(
        "/v1/invoke",
        json={"mandate_id": m1_id, "tool": "search", "arguments": {"q": "x"}},
        headers=hb,
    )
    assert steal_invoke.status_code == 404

    good = client.post(
        "/v1/invoke",
        json={"mandate_id": m1_id, "tool": "search", "arguments": {"q": "ok"}, "idempotency_key": "t-a-1"},
        headers=ha,
    )
    assert good.status_code == 200
    rec_id = good.json()["receipt"]["id"]
    assert client.get(f"/v1/receipts/{rec_id}", headers=hb).status_code == 404
    assert client.get(f"/v1/receipts/{rec_id}", headers=ha).status_code == 200
    b_recs = client.get("/v1/receipts", headers=hb).json()
    assert rec_id not in {r["id"] for r in b_recs}


def test_dashboard_never_query_string_key(cx):
    client = TestClient(app)
    assert client.get("/?key=dev").status_code == 401
