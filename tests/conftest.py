from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["CROSSING_ALLOW_DEV"] = "1"
os.environ.pop("STRIPE_SECRET_KEY", None)

import pytest

from crossing import crypto, db
from crossing.sdk import Crossing


@pytest.fixture
def cx(tmp_path):
    crypto.reset_for_tests()
    db.reset_engine()
    url = f"sqlite:///{tmp_path / 'crossing.db'}"
    os.environ["DATABASE_URL"] = url
    return Crossing(database_url=url)


@pytest.fixture
def seeded(cx):
    p = cx.create_principal("Alice")
    a = cx.create_agent(p.id, "researcher")
    from datetime import datetime, timedelta, timezone

    m = cx.issue_mandate(
        p.id,
        a.id,
        spend_limit_cents=100,
        max_call_cents=100,
        tools=["search"],
        servers=["mock"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        max_subagent_budget_cents=80,
    )
    return cx, p, a, m
