"""Tenant API keys: hashed secrets, prefix lookup, constant-time compare."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from crossing.crypto import ProductionConfigError, allow_dev
from crossing.models import Account, ApiKey, Principal, new_id, utcnow
from crossing.policy import PolicyDenied, Reason

SCOPES = (
    "read",
    "invoke",
    "mandate:issue",
    "mandate:revoke",
    "billing:read",
    "admin",
)
CUSTOMER_SCOPES = ("read", "invoke", "mandate:issue", "mandate:revoke", "billing:read")
ADMIN_SCOPES = SCOPES
BOOTSTRAP_PREFIX = "cxk_test_boot"
BOOTSTRAP_SECRET = "dev"  # raw secret only in ALLOW_DEV bootstrap; never stored
SESSION_COOKIE = "crossing_session"


def key_pepper() -> bytes:
    raw = (os.environ.get("CROSSING_KEY_PEPPER") or "").strip()
    if raw:
        return raw.encode("utf-8")
    if allow_dev():
        return b"crossing-dev-pepper"
    raise ProductionConfigError("CROSSING_KEY_PEPPER is required unless CROSSING_ALLOW_DEV=1")


def hash_secret(secret: str) -> str:
    return hmac.new(key_pepper(), secret.encode("utf-8"), hashlib.sha256).hexdigest()


def secrets_match(presented: str, stored_hash: str) -> bool:
    digest = hash_secret(presented)
    return hmac.compare_digest(digest, stored_hash)


def _env_kind() -> str:
    return "test" if allow_dev() else "live"


def visible_prefix() -> str:
    return f"cxk_{_env_kind()}_{secrets.token_hex(4)}"


def parse_prefix(presented: str) -> str | None:
    parts = (presented or "").split("_")
    if len(parts) >= 4 and parts[0] == "cxk" and parts[1] in ("live", "test"):
        return "_".join(parts[:3])
    return None


@dataclass
class IssuedKey:
    record: ApiKey
    secret: str


@dataclass
class AuthContext:
    account_id: str
    principal_id: str | None
    api_key_id: str
    kind: str
    scopes: list[str]
    prefix: str

    def has_scope(self, scope: str) -> bool:
        if "admin" in self.scopes:
            return True
        return scope in self.scopes

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin" or "admin" in self.scopes


def _scopes_json(scopes: list[str] | tuple[str, ...] | None, *, kind: str) -> str:
    if scopes is None:
        scopes = ADMIN_SCOPES if kind == "admin" else CUSTOMER_SCOPES
    cleaned = [s for s in scopes if s in SCOPES]
    if not cleaned:
        cleaned = list(CUSTOMER_SCOPES)
    return json.dumps(cleaned)


def create_account(session: Session, name: str) -> Account:
    acct = Account(id=new_id(), name=name)
    session.add(acct)
    session.flush()
    return acct


def issue_api_key(
    session: Session,
    *,
    account_id: str,
    kind: str = "customer",
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    prefix: str | None = None,
    secret: str | None = None,
) -> IssuedKey:
    if kind not in ("customer", "admin"):
        raise PolicyDenied(Reason.UNAUTHORIZED, "invalid key kind")
    if session.get(Account, account_id) is None:
        raise PolicyDenied(Reason.PRINCIPAL_MISSING, "account missing")
    prefix = prefix or visible_prefix()
    raw = secret if secret is not None else f"{prefix}_{secrets.token_hex(24)}"
    row = ApiKey(
        id=new_id(),
        account_id=account_id,
        prefix=prefix,
        secret_hash=hash_secret(raw),
        scopes_json=_scopes_json(scopes, kind=kind),
        revoked_at=None,
        expires_at=expires_at,
        last_used_at=None,
        kind=kind,
    )
    session.add(row)
    session.flush()
    return IssuedKey(record=row, secret=raw)


def rotate_api_key(session: Session, key_id: str) -> IssuedKey:
    row = session.get(ApiKey, key_id)
    if row is None:
        raise PolicyDenied(Reason.UNAUTHORIZED, "key missing")
    row.revoked_at = utcnow()
    session.flush()
    return issue_api_key(
        session,
        account_id=row.account_id,
        kind=row.kind,
        scopes=json.loads(row.scopes_json or "[]"),
    )


def revoke_api_key(session: Session, key_id: str) -> ApiKey:
    row = session.get(ApiKey, key_id)
    if row is None:
        raise PolicyDenied(Reason.UNAUTHORIZED, "key missing")
    row.revoked_at = utcnow()
    session.flush()
    return row


def _principal_for_account(session: Session, account_id: str) -> Principal | None:
    return session.scalar(select(Principal).where(Principal.account_id == account_id))


def _load_live_key(session: Session, row: ApiKey, presented: str) -> AuthContext | None:
    if row.revoked_at is not None:
        return None
    exp = row.expires_at
    if exp is not None:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if utcnow() > exp:
            return None
    if not secrets_match(presented, row.secret_hash):
        return None
    row.last_used_at = utcnow()
    session.flush()
    scopes = json.loads(row.scopes_json or "[]")
    principal = _principal_for_account(session, row.account_id)
    return AuthContext(
        account_id=row.account_id,
        principal_id=principal.id if principal is not None else None,
        api_key_id=row.id,
        kind=row.kind,
        scopes=list(scopes),
        prefix=row.prefix,
    )


def authenticate(session: Session, presented: str | None) -> AuthContext | None:
    if not presented:
        return None
    prefix = parse_prefix(presented)
    row = None
    if prefix:
        row = session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if row is None and allow_dev() and presented == BOOTSTRAP_SECRET:
        row = session.scalar(select(ApiKey).where(ApiKey.prefix == BOOTSTRAP_PREFIX))
    if row is None:
        return None
    return _load_live_key(session, row, presented)


def ensure_bootstrap(session: Session) -> None:
    """Mint a local admin key with raw secret 'dev' when ALLOW_DEV=1. Hash only is stored."""
    if not allow_dev():
        return
    existing = session.scalar(select(ApiKey).where(ApiKey.prefix == BOOTSTRAP_PREFIX))
    if existing is not None:
        return
    acct = create_account(session, "dev-bootstrap")
    issue_api_key(
        session,
        account_id=acct.id,
        kind="admin",
        scopes=list(ADMIN_SCOPES),
        prefix=BOOTSTRAP_PREFIX,
        secret=BOOTSTRAP_SECRET,
    )


def public_key_view(row: ApiKey, *, secret: str | None = None) -> dict:
    data = {
        "id": row.id,
        "account_id": row.account_id,
        "prefix": row.prefix,
        "scopes": json.loads(row.scopes_json or "[]"),
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "kind": row.kind,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if secret is not None:
        data["secret"] = secret
    return data
