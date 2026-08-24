from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from sqlalchemy.orm import Session

from crossing import crypto
from crossing.models import Receipt, new_id, utcnow


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(obj: Any) -> str:
    return hashlib.sha256(crypto.canonical_dumps(obj)).hexdigest()


def retain_payloads() -> bool:
    return os.environ.get("CROSSING_RETAIN_PAYLOADS", "") == "1"


def issue(
    session: Session,
    *,
    principal_id: str,
    mandate_id: str,
    reservation_id: str | None,
    tool: str,
    server: str,
    amount_cents: int,
    result: Any,
    agent_id: str | None = None,
    task_id: str | None = None,
    request_hash: str | None = None,
    outcome: str = "ok",
) -> Receipt:
    rid = new_id()
    req_hash = request_hash or payload_hash({"mandate_id": mandate_id, "server": server, "tool": tool})
    resp_hash = payload_hash(result)
    body: dict[str, Any] = {
        "v": 1,
        "id": rid,
        "agent_id": agent_id,
        "task_id": task_id,
        "mandate_id": mandate_id,
        "tool": tool,
        "server": server,
        "amount_cents": amount_cents,
        "outcome": outcome,
        "reservation_id": reservation_id,
        "request_hash": req_hash,
        "response_hash": resp_hash,
        "kid": crypto.key_id(),
        "issued_at": utcnow().isoformat(),
    }
    if retain_payloads():
        body["result"] = result
    sig = crypto.sign_obj(body)
    rec = Receipt(
        id=rid,
        principal_id=principal_id,
        mandate_id=mandate_id,
        agent_id=agent_id,
        task_id=task_id,
        reservation_id=reservation_id,
        tool=tool,
        server=server,
        amount_cents=amount_cents,
        body_json=_dumps(body),
        signature=sig,
        pubkey_hex=crypto.pubkey_hex(),
    )
    session.add(rec)
    session.flush()
    return rec


def verify_receipt(
    receipt: Receipt | dict[str, Any],
    *,
    body: dict[str, Any] | None = None,
    keys: dict[str, str] | None = None,
) -> bool:
    """Verify a receipt against Crossing issuer keys, never against a key inside the artifact.

    `keys` is kid -> pubkey_hex. Default is this process's issuer key. A third party
    should pass the document from GET /.well-known/crossing-keys (map kid to pubkey_hex).
    """
    if isinstance(receipt, dict):
        payload = body or receipt.get("body") or {
            k: v for k, v in receipt.items() if k not in ("signature", "pubkey_hex")
        }
        signature = receipt.get("signature")
        carried = receipt.get("pubkey_hex")
    else:
        payload = body if body is not None else receipt.body_obj()
        signature = receipt.signature
        carried = receipt.pubkey_hex
    if not isinstance(payload, dict) or not signature:
        return False
    kid = payload.get("kid")
    directory = dict(keys) if keys is not None else crypto.issuer_keys()
    if not kid or kid not in directory:
        return False
    anchored = directory[kid]
    if carried and carried != anchored:
        return False
    return crypto.verify_obj(payload, signature, anchored)


def to_dict(receipt: Receipt) -> dict[str, Any]:
    return {
        "id": receipt.id,
        "principal_id": receipt.principal_id,
        "mandate_id": receipt.mandate_id,
        "agent_id": receipt.agent_id,
        "task_id": receipt.task_id,
        "reservation_id": receipt.reservation_id,
        "tool": receipt.tool,
        "server": receipt.server,
        "amount_cents": receipt.amount_cents,
        "body": receipt.body_obj(),
        "signature": receipt.signature,
        "pubkey_hex": receipt.pubkey_hex,
        "kid": receipt.body_obj().get("kid"),
        "created_at": receipt.created_at.isoformat() if receipt.created_at else None,
    }
