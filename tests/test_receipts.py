from __future__ import annotations

from crossing import crypto
from crossing.receipts import verify_receipt


def test_tampered_receipt(seeded):
    cx, _, _, m = seeded
    r = cx.invoke(m.id, "search", {"q": "ok"}, idempotency_key="rec-1")
    assert r.ok
    assert verify_receipt(r.receipt)
    body = dict(r.receipt["body"])
    body["amount_cents"] = 99999
    assert crypto.verify_obj(body, r.receipt["signature"], r.receipt["pubkey_hex"]) is False
    tampered = dict(r.receipt)
    tampered["body"] = body
    assert verify_receipt(tampered) is False
