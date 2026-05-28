"""Tests for matrix.observability.logging_integration.

Verifies that ``install_log_correlation`` injects ``otelTraceID`` /
``otelSpanID`` onto LogRecords produced inside an active OTEL span, and
that records produced outside any span are not annotated.  Also verifies
that the existing JSON formatter includes the IDs in its output when they
are present.
"""

from __future__ import annotations

import json
import logging

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.trace import TracerProvider

from primer.common.log import _JsonFormatter
from primer.observability.logging_integration import install_log_correlation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CapturingHandler(logging.Handler):
    """Accumulates LogRecord instances for assertion."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def _isolated_otel_provider():
    """Each test gets a fresh TracerProvider so spans are isolated."""
    provider = TracerProvider()
    otel_trace.set_tracer_provider(provider)
    yield provider
    # Uninstrument after each test so the record factory is reset, preventing
    # state leak between tests.
    try:
        LoggingInstrumentor().uninstrument()
    except Exception:
        pass


@pytest.fixture()
def tracer(_isolated_otel_provider: TracerProvider):
    return _isolated_otel_provider.get_tracer("primer.test")


@pytest.fixture()
def capturing_handler() -> _CapturingHandler:
    handler = _CapturingHandler()
    handler.setLevel(logging.DEBUG)
    return handler


@pytest.fixture()
def test_logger(capturing_handler: _CapturingHandler) -> logging.Logger:
    lg = logging.getLogger("primer.test.log_correlation")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(capturing_handler)
    lg.propagate = False
    yield lg
    lg.removeHandler(capturing_handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLogRecordOutsideSpan:
    def test_no_trace_id_outside_span(
        self,
        test_logger: logging.Logger,
        capturing_handler: _CapturingHandler,
    ) -> None:
        """Records emitted outside any span must not carry otelTraceID."""
        install_log_correlation()

        test_logger.info("outside span message")

        assert len(capturing_handler.records) == 1
        record = capturing_handler.records[0]
        # The attribute should be absent — the hook only runs inside spans.
        assert not hasattr(record, "otelTraceID"), (
            "otelTraceID should not be set outside an active span"
        )
        assert not hasattr(record, "otelSpanID"), (
            "otelSpanID should not be set outside an active span"
        )


class TestLogRecordInsideSpan:
    def test_trace_id_matches_active_span(
        self,
        tracer,
        test_logger: logging.Logger,
        capturing_handler: _CapturingHandler,
    ) -> None:
        """Record emitted inside a span must carry otelTraceID matching the span."""
        install_log_correlation()

        with tracer.start_as_current_span("test-span") as span:
            test_logger.info("inside span message")
            ctx = span.get_span_context()
            expected_trace_id = format(ctx.trace_id, "032x")
            expected_span_id = format(ctx.span_id, "016x")

        assert len(capturing_handler.records) == 1
        record = capturing_handler.records[0]

        assert hasattr(record, "otelTraceID"), "otelTraceID should be set inside a span"
        assert hasattr(record, "otelSpanID"), "otelSpanID should be set inside a span"
        assert record.otelTraceID == expected_trace_id
        assert record.otelSpanID == expected_span_id

    def test_span_id_is_hex_string(
        self,
        tracer,
        test_logger: logging.Logger,
        capturing_handler: _CapturingHandler,
    ) -> None:
        """otelTraceID and otelSpanID must be lowercase hex strings."""
        install_log_correlation()

        with tracer.start_as_current_span("test-span"):
            test_logger.info("hex check")

        record = capturing_handler.records[0]
        # 32-char hex for trace_id, 16-char hex for span_id
        assert len(record.otelTraceID) == 32
        assert len(record.otelSpanID) == 16
        assert all(c in "0123456789abcdef" for c in record.otelTraceID)
        assert all(c in "0123456789abcdef" for c in record.otelSpanID)


class TestFormatterIncludesTraceId:
    def test_json_output_contains_trace_id_inside_span(
        self,
        tracer,
        test_logger: logging.Logger,
        capturing_handler: _CapturingHandler,
    ) -> None:
        """JSON-formatted log line inside a span must include otelTraceID."""
        install_log_correlation()
        formatter = _JsonFormatter()
        capturing_handler.setFormatter(formatter)

        with tracer.start_as_current_span("test-span") as span:
            test_logger.info("formatted inside span")
            ctx = span.get_span_context()
            expected_trace_id = format(ctx.trace_id, "032x")

        assert len(capturing_handler.records) == 1
        raw = formatter.format(capturing_handler.records[0])
        payload = json.loads(raw)

        assert "otelTraceID" in payload, "JSON output must contain otelTraceID"
        assert payload["otelTraceID"] == expected_trace_id
        assert "otelSpanID" in payload, "JSON output must contain otelSpanID"

    def test_json_output_omits_trace_id_outside_span(
        self,
        test_logger: logging.Logger,
        capturing_handler: _CapturingHandler,
    ) -> None:
        """JSON-formatted log line outside any span must not include otelTraceID."""
        install_log_correlation()
        formatter = _JsonFormatter()
        capturing_handler.setFormatter(formatter)

        test_logger.info("formatted outside span")

        assert len(capturing_handler.records) == 1
        raw = formatter.format(capturing_handler.records[0])
        payload = json.loads(raw)

        assert "otelTraceID" not in payload, (
            "otelTraceID must be absent from JSON output outside a span"
        )
        assert "otelSpanID" not in payload, (
            "otelSpanID must be absent from JSON output outside a span"
        )
