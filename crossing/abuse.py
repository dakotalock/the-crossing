"""Rate limit (single-instance), payload size, argument schema. Not multi-host."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from crossing.mock_mcp import TOOLS
from crossing.policy import PolicyDenied, Reason

_hits: dict[str, list[float]] = {}


def max_body_bytes() -> int:
    try:
        return max(1024, int(os.environ.get("CROSSING_MAX_BODY_BYTES") or 65536))
    except ValueError:
        return 65536


def max_args_bytes() -> int:
    try:
        return max(256, int(os.environ.get("CROSSING_MAX_ARGS_BYTES") or 16384))
    except ValueError:
        return 16384


def rate_limit_per_minute() -> int:
    try:
        return max(0, int(os.environ.get("CROSSING_RATE_LIMIT_PER_MINUTE") or "120"))
    except ValueError:
        return 120


def reset_for_tests() -> None:
    _hits.clear()


def check_rate_limit(key_id: str) -> None:
    """In-memory sliding window. Documented as not multi-host."""
    limit = rate_limit_per_minute()
    if limit <= 0:
        return
    now = time.monotonic()
    window = _hits.setdefault(key_id, [])
    cutoff = now - 60.0
    window[:] = [t for t in window if t > cutoff]
    if len(window) >= limit:
        raise PolicyDenied(Reason.RATE_LIMITED, "per-key rate limit")
    window.append(now)


def _walk(obj: Any, *, depth: int) -> None:
    if depth > 6:
        raise PolicyDenied(Reason.INVALID_ARGUMENTS, "arguments too nested")
    if isinstance(obj, dict):
        if len(obj) > 32:
            raise PolicyDenied(Reason.INVALID_ARGUMENTS, "too many keys")
        for k, v in obj.items():
            if not isinstance(k, str) or len(k) > 64:
                raise PolicyDenied(Reason.INVALID_ARGUMENTS, "invalid key")
            _walk(v, depth=depth + 1)
    elif isinstance(obj, list):
        if len(obj) > 32:
            raise PolicyDenied(Reason.INVALID_ARGUMENTS, "too many items")
        for v in obj:
            _walk(v, depth=depth + 1)
    elif isinstance(obj, str):
        if len(obj) > 4096:
            raise PolicyDenied(Reason.INVALID_ARGUMENTS, "string too long")
    elif isinstance(obj, (int, float, bool)) or obj is None:
        return
    else:
        raise PolicyDenied(Reason.INVALID_ARGUMENTS, "unsupported type")


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def validate_arguments(tool: str, arguments: dict[str, Any] | None) -> None:
    arguments = arguments or {}
    raw = json.dumps(arguments, separators=(",", ":"), ensure_ascii=True)
    if len(raw.encode("utf-8")) > max_args_bytes():
        raise PolicyDenied(Reason.PAYLOAD_TOO_LARGE, "arguments exceed size limit")
    if not isinstance(arguments, dict):
        raise PolicyDenied(Reason.INVALID_ARGUMENTS, "arguments must be an object")
    _walk(arguments, depth=0)
    spec = TOOLS.get(tool)
    if spec is None:
        return
    schema = spec.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    for key in required:
        if key not in arguments:
            raise PolicyDenied(Reason.INVALID_ARGUMENTS, f"missing {key}")
    for key, value in arguments.items():
        if key not in props:
            raise PolicyDenied(Reason.INVALID_ARGUMENTS, f"unknown argument {key}")
        expected = (props.get(key) or {}).get("type")
        if expected and not _type_ok(value, expected):
            raise PolicyDenied(Reason.INVALID_ARGUMENTS, f"{key} type")
