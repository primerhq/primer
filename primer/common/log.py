"""Stdlib logging configuration for primer.

Single configuration entry point. The library never auto-configures
logging; the application calls :func:`configure_logging` once at startup.
Per-file pattern: every code file in ``primer/`` does
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
call in primer code AND in dependencies (openai, anthropic, google.genai,
ollama, httpx, etc.) inherits this configuration. The application can
silence or re-route specific logger names afterwards via stdlib
``logging``.

Credentials carried in URLs (``?key=``, ``?token=``, the ``user:password@``
of a Base URL, Telegram ``/bot<token>/``, webhook ``/v1/webhooks/<token>``)
are masked on the configured handler and on uvicorn's self-handled loggers, so the httpx
INFO request line and the uvicorn access line never write them out.
"""

from __future__ import annotations

import base64
import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Standard LogRecord attributes — anything else on the record came from
# ``extra={...}`` and should be emitted as a top-level JSON field.
_RESERVED_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "asctime", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info", "lineno",
    "funcName", "created", "msecs", "relativeCreated", "thread",
    "threadName", "processName", "process", "message", "taskName",
})


# Credentials that travel in URLs (SEC-06). httpx logs every request URL
# at INFO and uvicorn logs every request path, so a URL-borne credential
# would otherwise land in the server log on every call.
_URL_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Query-string credentials: ?key=..., &access_token=..., etc.
    (
        re.compile(
            r"(?i)([?&](?:key|api_key|apikey|api-key|token|access_token|"
            r"refresh_token|id_token|client_secret|secret|password)=)"
            r"[^&#\s'\"<>]+"
        ),
        r"\1[REDACTED]",
    ),
    # Telegram Bot API: https://api.telegram.org/bot<id>:<secret>/method
    (re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+"), r"\1[REDACTED]"),
    # Webhook capability tokens: keep the last 4 chars for correlation.
    (
        re.compile(r"(/v1/webhooks/)[A-Za-z0-9_-]*([A-Za-z0-9_-]{4})\b"),
        r"\1***\2",
    ),
)

# Userinfo of a URL: https://user:password@host/... httpx prints a Base URL whole, credentials included, in an error ("Server error '500 ...' for url
# '...'") and in its INFO request line. The userinfo runs to the LAST "@" before the first "/", "?" or "#", because a password may hold an
# apostrophe and, in a hand-built string, a raw "@". An "@" after the authority (a path, a query, a fragment) is not userinfo. The scheme and the host
# stay.
#
# The scheme is `[a-z][a-z0-9]*`, NOT `[a-z][a-z0-9+.-]*`: with "+", "." and "-" in the class every letter after one of them is a new word start (\b), so
# a run like "a.a.a." is rescanned from each start and the match is QUADRATIC (100k characters took 24 s). This runs on every log record, including
# uvicorn.access, which logs the request path of an unauthenticated request. Without the extras the output is the same for git+ssh, mongodb+srv and
# postgresql+asyncpg: the scheme is kept as it is and only the part after the last "+" is matched.
_USERINFO = re.compile(r'(\b[a-z][a-z0-9]*://)[^/?#\s"]*@', re.IGNORECASE)


def redact_url_secrets(text: str) -> str:
    """Mask URL-borne credentials (userinfo, query keys, Telegram bot
    tokens, webhook capability tokens) in ``text``."""
    # Userinfo needs both "://" and "@": a substring test is far cheaper than the regex on the text of every record.
    if "@" in text and "://" in text:
        text = _USERINFO.sub(r"\1[REDACTED]@", text)
    for pattern, repl in _URL_SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


# Bearer and Basic tokens in free text (the Authorization header a library echoes back in an error, a provider quoting the credential it received).
# A Bearer token is masked from 8 characters up. A ``Basic`` token is masked only when it base64-decodes to printable text containing a ``:``
# (``user:password``), so the words after "Basic" in prose ("Basic authentication is required") are left alone.
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_BASIC = re.compile(r"(?i)(\bbasic\s+)([A-Za-z0-9+/]{4,}={0,2})")
_REDACTED = "[REDACTED]"


def _mask_basic(match: re.Match[str]) -> str:
    """``Basic <token>`` with the token masked when it is base64 of printable ``user:password``-shaped text; any other word is left as it was."""
    token = match.group(2)
    try:
        decoded = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("utf-8")
    except ValueError:                                  # not base64 (binascii.Error), or not text (UnicodeDecodeError): both are ValueErrors
        return match.group(0)
    return match.group(1) + _REDACTED if ":" in decoded and decoded.isprintable() else match.group(0)


def redact_credentials(text: str) -> str:
    """``text`` with the credentials a failure message can carry masked: URL-borne ones (:func:`redact_url_secrets`) and Bearer / Basic tokens.

    For text that came from a library or a peer and is about to be stored, shown to a model or returned over MCP (a tool's exception, a child agent's
    or graph's failure body). It knows no provider, so it cannot mask a configured key that is not URL- or header-shaped; the LLM adapters do that
    with ``primer.llm._failure.scrub``, which ends in this function.
    """
    return _BASIC.sub(_mask_basic, _BEARER.sub(r"\1" + _REDACTED, redact_url_secrets(text)))


def _redact_arg(arg: Any) -> Any:
    if arg is None or isinstance(arg, (bool, int, float)):
        return arg
    try:
        text = str(arg)
    except Exception:  # noqa: BLE001 - a raising __str__ is the formatter's to report
        # Filters run outside Handler.handleError's guard, so a raise
        # here would escape to the logger.info() caller. Keep the arg.
        return arg
    redacted = redact_url_secrets(text)
    # Only replace the arg when something was masked, so %r / %d
    # formatting of ordinary args is untouched.
    return redacted if redacted != text else arg


class _UrlSecretFilter(logging.Filter):
    """Masks URL-borne credentials in a record's message, args and
    exception text before any formatter sees it.

    Rewrites args element-wise first rather than collapsing them into
    the message: uvicorn's AccessFormatter unpacks a 5-tuple of args.
    Only a credential that spans the format string and its args is
    caught by collapsing the formatted message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Filters run outside Handler.handleError's guard: nothing in here
        # may raise into the caller of logger.info().
        try:
            self._redact(record)
        except Exception:  # noqa: BLE001 - never break the caller's log call
            pass
        return True

    def _redact(self, record: logging.LogRecord) -> None:
        if isinstance(record.msg, str):
            record.msg = redact_url_secrets(record.msg)
        elif not record.args:
            # logger.warning(exc): the message is the object's str().
            record.msg = _redact_arg(record.msg)
        # String extras (extra={"path": request.url.path}) are emitted
        # verbatim by _JsonFormatter.
        for key, value in list(record.__dict__.items()):
            # '_'-prefixed attributes are private to a handler or library
            # and never emitted by _JsonFormatter.
            if (
                key in _RESERVED_RECORD_ATTRS
                or key.startswith("_")
                or not isinstance(value, str)
            ):
                continue
            record.__dict__[key] = redact_url_secrets(value)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        if record.args:
            # A URL split across the format string and its args (e.g.
            # "...?%s=%s") is only visible once formatted: collapse it.
            # uvicorn's access args are whole URLs, so they never get here.
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 - a bad format is logging's to report
                message = None
            if message is not None:
                redacted = redact_url_secrets(message)
                if redacted != message:
                    record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(
                record.exc_info,
            )
        if record.exc_text:
            record.exc_text = redact_url_secrets(record.exc_text)


_URL_SECRET_FILTER = _UrlSecretFilter()

# Loggers that write through their own handlers (propagate=False under
# uvicorn's logging config), so the root handler's filter never sees
# their records: the filter goes on the logger itself.
_SELF_HANDLED_LOGGERS = ("uvicorn.access", "uvicorn.error")


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
            # exc_text is the (redacted) text _UrlSecretFilter cached.
            payload["traceback"] = (
                record.exc_text or self.formatException(record.exc_info)
            )
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
    file_path: str | Path | None = None,
    file_max_bytes: int = 10 * 1024 * 1024,
    file_backup_count: int = 5,
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
    file_path
        When provided, logs are written to a rotating file at this path
        instead of stderr. The parent directory is created on demand.
        When ``None`` (default), logs continue to go to stderr.
    file_max_bytes
        Per-file rotation size cap. Only consulted when ``file_path`` is set.
    file_backup_count
        Number of rotated backups to retain. Only consulted when
        ``file_path`` is set.
    """
    root = logging.getLogger()
    # Idempotent — drop existing handlers rather than stacking new ones.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler: logging.Handler
    if file_path is not None:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=file_max_bytes,
            backupCount=file_backup_count,
            encoding="utf-8",
        )
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if json_format else _DevFormatter())
    handler.addFilter(_URL_SECRET_FILTER)
    root.addHandler(handler)
    for name in _SELF_HANDLED_LOGGERS:
        logger_ = logging.getLogger(name)
        if _URL_SECRET_FILTER not in logger_.filters:
            logger_.addFilter(_URL_SECRET_FILTER)
    root.setLevel(level)

    # Per-library noise floors: even at DEBUG, these libraries produce
    # one log line per SQL statement / per HTTP frame which drowns out
    # the primer-level signal that operators actually use. Pin them at
    # INFO regardless of the application level. Override by setting the
    # logger explicitly elsewhere if you really want the firehose.
    _noisy_loggers = ("aiosqlite", "asyncio", "httpcore", "httpx")
    for name in _noisy_loggers:
        logging.getLogger(name).setLevel(max(level, logging.INFO))
