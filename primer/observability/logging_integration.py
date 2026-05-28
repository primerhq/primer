"""OTEL log-correlation: inject trace_id + span_id into every LogRecord.

Usage
-----
Call ``install_log_correlation()`` once during application lifespan startup,
*after* the TracerProvider has been configured.  Every log record emitted
while a span is active will have ``otelTraceID`` and ``otelSpanID`` attached
as plain string attributes.

The existing :class:`matrix.common.log._JsonFormatter` already emits any
non-reserved ``LogRecord`` attribute as a top-level JSON field, so no
further changes to the formatter are required — the IDs appear
automatically in the JSON output.

When *no* span is active the attributes are not added, keeping logs outside
traces clean.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.trace import Span

logger = logging.getLogger(__name__)


def _log_hook(span: "Span", record: logging.LogRecord) -> None:  # noqa: ARG001
    """Hook called by LoggingInstrumentor for every record inside a span.

    Injects ``otelTraceID`` and ``otelSpanID`` as hex strings onto the
    record so downstream formatters (e.g. the JSON formatter) can pick
    them up transparently.
    """
    ctx = span.get_span_context()
    record.otelTraceID = format(ctx.trace_id, "032x")
    record.otelSpanID = format(ctx.span_id, "016x")


def install_log_correlation() -> None:
    """Install OTEL log correlation.

    Registers a :class:`~opentelemetry.instrumentation.logging.LoggingInstrumentor`
    with ``set_logging_format=False`` so the existing log formatter is left
    intact.  A ``log_hook`` injects ``otelTraceID`` / ``otelSpanID`` onto
    every :class:`logging.LogRecord` produced while an active span is in
    scope.  Records produced outside any span are left unchanged.

    Safe to call multiple times: subsequent calls are no-ops (the
    instrumentor guards against double-installation).
    """
    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(
            set_logging_format=False,
            log_hook=_log_hook,
        )
        logger.debug("logging_integration: log-correlation hook installed")
    except Exception:
        logger.exception(
            "logging_integration: failed to install log-correlation hook; "
            "trace_id will not appear in logs"
        )


__all__ = ["install_log_correlation"]
