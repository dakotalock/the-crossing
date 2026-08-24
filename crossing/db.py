"""Engine, sessions, SQLite BEGIN IMMEDIATE."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from crossing.models import Base

DEFAULT_URL = "sqlite:///./crossing.db"

_engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("CROSSING_DATABASE_URL") or DEFAULT_URL


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def make_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    kwargs: dict = {"future": True}
    if _is_sqlite(url):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15.0}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    if not _is_sqlite(url):
        kwargs.setdefault("pool_pre_ping", True)
    engine = create_engine(url, **kwargs)

    # BEGIN IMMEDIATE is SQLite-only. Postgres uses default isolation;
    # scarce-authority gates are conditional UPDATE ... RETURNING.
    if _is_sqlite(url):

        @event.listens_for(engine, "connect")
        def _connect(dbapi_connection, _connection_record):  # noqa: ANN001
            dbapi_connection.isolation_level = None
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA busy_timeout=8000")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
            finally:
                cur.close()

        @event.listens_for(engine, "begin")
        def _begin(conn):  # noqa: ANN001
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


REQUIRED_TABLES = (
    "accounts",
    "principals",
    "agents",
    "mandates",
    "ledger_events",
    "reservations",
    "receipts",
    "billing_outbox",
    "idempotency_records",
    "used_nonces",
    "invocations",
    "api_keys",
    "reconciliation_events",
)


def schema_ready(engine: Engine) -> bool:
    insp = inspect(engine)
    names = set(insp.get_table_names())
    return all(t in names for t in REQUIRED_TABLES)


def init_db(url: str | None = None) -> Engine:
    """Dev sqlite may create_all. Production requires Alembic-applied schema."""
    global _engine, SessionLocal
    from crossing.crypto import ProductionConfigError, allow_dev

    _engine = make_engine(url)
    if allow_dev():
        Base.metadata.create_all(_engine)
    elif not schema_ready(_engine):
        raise ProductionConfigError(
            "database schema missing; run `alembic upgrade head` "
            "(create_all is disabled when CROSSING_ALLOW_DEV!=1)"
        )
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=True, future=True)
    if allow_dev():
        from crossing.auth import ensure_bootstrap

        session = SessionLocal()
        try:
            ensure_bootstrap(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        return init_db()
    return _engine


def get_session() -> Session:
    if SessionLocal is None:
        init_db()
    assert SessionLocal is not None
    return SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    global _engine, SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    SessionLocal = None
