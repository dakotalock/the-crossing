"""Ed25519 keys and signatures. Production refuses a missing seed."""

from __future__ import annotations

import os
import json
from typing import Any

from nacl.encoding import HexEncoder, RawEncoder
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

_ALLOW_DEV = "CROSSING_ALLOW_DEV"
_SEED_ENV = "CROSSING_ED25519_SEED"

_state: dict[str, Any] = {}


class ProductionConfigError(RuntimeError):
    pass


def allow_dev() -> bool:
    return os.environ.get(_ALLOW_DEV, "") == "1"


def reset_for_tests() -> None:
    _state.clear()


def _parse_seed(raw: str) -> bytes:
    raw = (raw or "").strip()
    if len(raw) != 64:
        raise ProductionConfigError("CROSSING_ED25519_SEED must be 64 hex characters")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise ProductionConfigError("CROSSING_ED25519_SEED must be 64 hex characters") from exc


def require_production_secrets() -> None:
    if allow_dev():
        return
    seed = os.environ.get(_SEED_ENV) or ""
    _parse_seed(seed)
    if not (os.environ.get("CROSSING_KEY_PEPPER") or "").strip():
        raise ProductionConfigError("CROSSING_KEY_PEPPER is required unless CROSSING_ALLOW_DEV=1")


def signing_key() -> SigningKey:
    if "sk" in _state:
        return _state["sk"]
    seed = os.environ.get(_SEED_ENV) or ""
    if seed:
        sk = SigningKey(_parse_seed(seed))
    elif allow_dev():
        sk = SigningKey.generate()
    else:
        raise ProductionConfigError(
            "CROSSING_ED25519_SEED is required unless CROSSING_ALLOW_DEV=1"
        )
    _state["sk"] = sk
    _state["vk"] = sk.verify_key
    return sk


def verify_key() -> VerifyKey:
    signing_key()
    return _state["vk"]


def pubkey_hex() -> str:
    return verify_key().encode(encoder=HexEncoder).decode("ascii")


def canonical_dumps(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sign_obj(obj: Any) -> str:
    signed = signing_key().sign(canonical_dumps(obj), encoder=RawEncoder)
    return signed.signature.hex()


def verify_obj(obj: Any, signature_hex: str, pubkey_hex_str: str | None = None) -> bool:
    try:
        sig = bytes.fromhex(signature_hex)
        vk = VerifyKey(bytes.fromhex(pubkey_hex_str), encoder=RawEncoder) if pubkey_hex_str else verify_key()
        vk.verify(canonical_dumps(obj), sig)
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False
