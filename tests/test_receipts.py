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


def test_foreign_keypair_receipt_rejected():
    from hashlib import sha256

    from nacl.encoding import HexEncoder
    from nacl.signing import SigningKey

    sk = SigningKey.generate()
    pk = sk.verify_key.encode(encoder=HexEncoder).decode("ascii")
    kid = "k1-" + sha256(pk.encode("ascii")).hexdigest()[:12]
    body = {
        "v": 1,
        "id": "forged",
        "mandate_id": "mandate-that-never-existed",
        "amount_cents": 9999999,
        "kid": kid,
        "issued_at": "2026-08-24T00:00:00+00:00",
    }
    sig = sk.sign(crypto.canonical_dumps(body)).signature.hex()
    receipt = {"body": body, "signature": sig, "pubkey_hex": pk, "kid": kid}
    assert crypto.verify_obj(body, sig, pk) is True
    assert verify_receipt(receipt) is False


def test_unknown_kid_rejected(seeded):
    cx, _, _, m = seeded
    r = cx.invoke(m.id, "search", {"q": "ok"}, idempotency_key="rec-kid")
    body = dict(r.receipt["body"])
    body["kid"] = "k1-not-in-directory"
    tampered = dict(r.receipt)
    tampered["body"] = body
    tampered["signature"] = crypto.sign_obj(body)
    tampered["pubkey_hex"] = crypto.pubkey_hex()
    assert verify_receipt(tampered) is False
