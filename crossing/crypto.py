"""Ed25519 keys and signatures. Production refuses a missing seed.

Key store is pluggable. The default EnvSeedKeyStore reads CROSSING_ED25519_SEED
(64 hex chars). Production recommendation: HSM/KMS (CROSSING_KEY_BACKEND=hsm is
not implemented here — wire a KeyStore that never materializes the seed in
process memory). Prod still refuses ephemeral/dev keys unless CROSSING_ALLOW_DEV=1.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Protocol

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
    backend = (os.environ.get("CROSSING_KEY_BACKEND") or "env").strip().lower()
    if backend == "hsm":
        raise ProductionConfigError(
            "CROSSING_KEY_BACKEND=hsm is not wired; keep env seed for beta or implement KeyStore"
        )


class KeyStore(Protocol):
    def signing_key(self) -> SigningKey: ...
    def verify_key(self) -> VerifyKey: ...
    def pubkey_hex(self) -> str: ...
    def kid(self) -> str: ...


class EnvSeedKeyStore:
    """Default store: seed from env. Do not log the seed."""

    def __init__(self) -> None:
        self._sk: SigningKey | None = None

    def signing_key(self) -> SigningKey:
        if self._sk is not None:
            return self._sk
        seed = os.environ.get(_SEED_ENV) or ""
        if seed:
            self._sk = SigningKey(_parse_seed(seed))
        elif allow_dev():
            self._sk = SigningKey.generate()
        else:
            raise ProductionConfigError(
                "CROSSING_ED25519_SEED is required unless CROSSING_ALLOW_DEV=1"
            )
        return self._sk

    def verify_key(self) -> VerifyKey:
        return self.signing_key().verify_key

    def pubkey_hex(self) -> str:
        return self.verify_key().encode(encoder=HexEncoder).decode("ascii")

    def kid(self) -> str:
        explicit = (os.environ.get("CROSSING_KEY_ID") or "").strip()
        if explicit:
            return explicit[:64]
        pk = self.pubkey_hex()
        digest = hashlib.sha256(pk.encode("ascii")).hexdigest()[:12]
        return "k1-" + digest


class HsmKeyStore:
    """Production recommendation: sign via HSM/KMS so the seed never sits in env.

    Not implemented in this beta. Selecting CROSSING_KEY_BACKEND=hsm fails closed.
    """

    def signing_key(self) -> SigningKey:
        raise ProductionConfigError("HSM key store is not implemented")

    def verify_key(self) -> VerifyKey:
        raise ProductionConfigError("HSM key store is not implemented")

    def pubkey_hex(self) -> str:
        raise ProductionConfigError("HSM key store is not implemented")

    def kid(self) -> str:
        raise ProductionConfigError("HSM key store is not implemented")


def key_store() -> KeyStore:
    if "store" in _state:
        return _state["store"]
    backend = (os.environ.get("CROSSING_KEY_BACKEND") or "env").strip().lower()
    if backend == "hsm":
        store: KeyStore = HsmKeyStore()
    else:
        store = EnvSeedKeyStore()
    _state["store"] = store
    return store


def signing_key() -> SigningKey:
    if "sk" in _state:
        return _state["sk"]
    sk = key_store().signing_key()
    _state["sk"] = sk
    _state["vk"] = sk.verify_key
    return sk


def verify_key() -> VerifyKey:
    signing_key()
    return _state["vk"]


def pubkey_hex() -> str:
    return verify_key().encode(encoder=HexEncoder).decode("ascii")


def key_id() -> str:
    if "kid" in _state:
        return _state["kid"]
    kid = key_store().kid()
    _state["kid"] = kid
    return kid


def issuer_keys() -> dict[str, str]:
    """kid -> pubkey_hex for keys this process will treat as Crossing issuers."""
    return {key_id(): pubkey_hex()}


def published_keys() -> dict[str, Any]:
    """Public key directory. A receipt is only valid if its kid is listed here."""
    return {
        "v": 1,
        "alg": "Ed25519",
        "keys": [{"kid": kid, "pubkey_hex": pk, "alg": "Ed25519"} for kid, pk in issuer_keys().items()],
    }


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
