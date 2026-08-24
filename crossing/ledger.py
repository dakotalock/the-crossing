"""Append-only ledger and atomic budget reservations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from crossing.models import IdempotencyRecord, Invocation, LedgerEvent, Mandate, Reservation, new_id
from crossing.policy import PolicyDenied, Reason
