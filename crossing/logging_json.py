"""JSON logs: request_id / invocation_id, never payloads or secrets."""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("crossing_request_id", default=None)
invocation_id_var: ContextVar[str | None] = ContextVar("crossing_invocation_id", default=None)

_SECRET_KEYS = (
    "secret",
    "token",
    "password",
    "authorization",
    "seed",
    "pepper",
    "stripe",
    "signature",
    "api_key",
    "apikey",
    "payload",
    "arguments",
    "result",
    "body",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "request_id", None) or request_id_var.get()
        iid = getattr(record, "invocation_id", None) or invocation_id_var.get()
        if rid:
            payload["request_id"] = rid
        if iid:
            payload["invocation_id"] = iid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging() -> None:
    if os.environ.get("CROSSING_JSON_LOGS", "1") == "0":
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    level = os.environ.get("CROSSING_LOG_LEVEL") or "INFO"
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in _SECRET_KEYS):
                out[k] = "[redacted]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj[:8]]
    return obj
