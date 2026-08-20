"""Structured JSON logging with a per-request id.

Deliberately never logs: API keys, Google credentials, or full document/OCR text.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

#: Keys that must never reach the log stream, whatever a caller passes.
_REDACTED_KEYS = {
    "api_key",
    "x-api-key",
    "authorization",
    "credentials",
    "google_application_credentials",
    "token",
    "raw_text",
    "ocr_text",
}

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

#: Custom fields travel under this single key. Passing them as top-level ``extra`` entries would
#: crash on any name the logging module reserves -- ``filename`` in particular, which this
#: service logs on every request.
_CONTEXT_KEY = "_context"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        fields: dict[str, Any] = dict(getattr(record, _CONTEXT_KEY, None) or {})
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            fields.setdefault(key, value)
        for key, value in fields.items():
            payload[key] = "[REDACTED]" if key.lower() in _REDACTED_KEYS else value
        if record.exc_info:
            # The message only; no traceback is ever returned to the client, but keeping the
            # type/message in logs is what makes production failures diagnosable.
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Uvicorn's own access log duplicates what we already emit structurally.
    logging.getLogger("uvicorn.access").propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit one structured event. Unknown keys are passed through, sensitive ones redacted."""
    logger.info(event, extra={_CONTEXT_KEY: fields})


def log_exception(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a failure event with the exception type/message kept for diagnosis."""
    logger.exception(event, extra={_CONTEXT_KEY: fields})
