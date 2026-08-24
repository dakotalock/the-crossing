from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from crossing import crypto
from crossing.models import Receipt, new_id, utcnow


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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
) -> Receipt:
    rid = new_id()
    body = {
        "v": 1,
        "id": rid,
        "principal_id": principal_id,
        "mandate_id": mandate_id,
        "reservation_id": reservation_id,
        "tool": tool,
        "server": server,
        "amount_cents": amount_cents,
        "result": result,
        "issued_at": utcnow().isoformat(),
    }
    sig = crypto.sign_obj(body)
    rec = Receipt(
        id=rid,
        principal_id=principal_id,
        mandate_id=mandate_id,
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


def verify_receipt(receipt: Receipt | dict[str, Any], *, body: dict[str, Any] | None = None) -> bool:
    if isinstance(receipt, dict):
        payload = body or receipt.get("body") or {k: v for k, v in receipt.items() if k not in ("signature", "pubkey_hex")}
        return crypto.verify_obj(payload, receipt["signature"], receipt.get("pubkey_hex"))
    payload = body if body is not None else receipt.body_obj()
    return crypto.verify_obj(payload, receipt.signature, receipt.pubkey_hex)


def to_dict(receipt: Receipt) -> dict[str, Any]:
    return {
        "id": receipt.id,
        "principal_id": receipt.principal_id,
        "mandate_id": receipt.mandate_id,
        "reservation_id": receipt.reservation_id,
        "tool": receipt.tool,
        "server": receipt.server,
        "amount_cents": receipt.amount_cents,
        "body": receipt.body_obj(),
        "signature": receipt.signature,
        "pubkey_hex": receipt.pubkey_hex,
        "created_at": receipt.created_at.isoformat() if receipt.created_at else None,
    }
