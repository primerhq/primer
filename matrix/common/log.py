"""Stdlib logging configuration for matrix.

Single configuration entry point. The library never auto-configures
logging; the application calls :func:`configure_logging` once at startup.
Per-file pattern: every code file in ``matrix/`` does
``logger = logging.getLogger(__name__)`` immediately after its imports.

Two output formats:

* **JSON** (default) — one self-contained JSON object per line. Safe for
  log aggregators. Carries ``timestamp`` (ISO 8601 UTC), ``level``,
  ``logger``, ``message``, plus any keyword passed via ``extra={...}``.
  Stack traces from ``logger.exception(...)`` land under ``traceback``.
* **Dev** — single-line human-readable
  ``<timestamp> [<level>] <logger>: <message>`` with stack traces inline.
  Intended for local hacking only.

Configuring the *root* logger means every ``logging.getLogger(name)``
call in matrix code AND in dependencies (openai, anthropic, google.genai,
ollama, httpx, etc.) inherits this configuration. The application can
silence or re-route specific logger names afterwards via stdlib
``logging``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


# Standard LogRecord attributes — anything else on the record came from
# ``extra={...}`` and should be emitted as a top-level JSON field.
_RESERVED_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "asctime", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info", "lineno",
    "funcName", "created", "msecs", "relativeCreated", "thread",
    "threadName", "processName", "process", "message", "taskName",
})


class _JsonFormatter(logging.Formatter):
    """Hand-rolled JSON log formatter — no extra runtime dependency."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Pull caller-provided extras (anything passed via extra={...}).
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            # Block payload-key collisions: 'level', 'logger', 'timestamp'
            # are payload keys but NOT in _RESERVED_RECORD_ATTRS, so they
            # would otherwise be silently overwritten by extras with the
            # same name.
            if key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _DevFormatter(logging.Formatter):
    """Human-readable single-line formatter for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )


def configure_logging(
    *,
    level: int = logging.INFO,
    json_format: bool = True,
) -> None:
    """Idempotent root-logger configuration.

    Replaces any existing handlers on the root logger so repeated calls
    don't stack up handlers. The application calls this once at startup;
    library code never calls it.

    Parameters
    ----------
    level
        Minimum log level the root logger emits. Defaults to ``INFO``.
    json_format
        When True (default), use the JSON formatter. When False, use the
        human-readable dev formatter.
    """
    root = logging.getLogger()
    # Idempotent — drop existing handlers rather than stacking new ones.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if json_format else _DevFormatter())
    root.addHandler(handler)
    root.setLevel(level)
