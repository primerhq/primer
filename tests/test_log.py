"""Unit tests for matrix.common.log.

Covers ``configure_logging()`` and the two formatters
(:class:`_JsonFormatter` and :class:`_DevFormatter`).
"""

from __future__ import annotations

import io
import json
import logging
import logging.handlers

import pytest

from primer.common.log import configure_logging


@pytest.fixture(autouse=True)
def _reset_logging():
    """Reset the root logger before and after each test so configurations
    don't leak between tests."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def _capture_to_buf(json_format: bool) -> tuple[logging.Logger, io.StringIO]:
    """Configure logging then attach an extra StringIO handler that uses
    the same formatter so we can capture log output for assertions."""
    configure_logging(json_format=json_format)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root_handler = logging.getLogger().handlers[0]
    handler.setFormatter(root_handler.formatter)
    logging.getLogger().addHandler(handler)
    return logging.getLogger("primer.test"), buf


# ============================================================================
# configure_logging — idempotency, level, format selection
# ============================================================================


class TestConfigureLogging:
    def test_default_invocation(self):
        configure_logging()
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert root.level == logging.INFO

    def test_idempotent_does_not_stack_handlers(self):
        configure_logging()
        configure_logging()
        configure_logging()
        assert len(logging.getLogger().handlers) == 1

    def test_level_kwarg(self):
        configure_logging(level=logging.DEBUG)
        assert logging.getLogger().level == logging.DEBUG

    def test_json_format_default_uses_json_formatter(self):
        configure_logging()
        root_handler = logging.getLogger().handlers[0]
        assert root_handler.formatter.__class__.__name__ == "_JsonFormatter"

    def test_dev_format_uses_dev_formatter(self):
        configure_logging(json_format=False)
        root_handler = logging.getLogger().handlers[0]
        assert root_handler.formatter.__class__.__name__ == "_DevFormatter"

    def test_default_handler_is_stream_handler(self):
        configure_logging()
        h = logging.getLogger().handlers[0]
        assert isinstance(h, logging.StreamHandler)
        assert not isinstance(h, logging.handlers.RotatingFileHandler)

    def test_file_path_uses_rotating_file_handler(self, tmp_path):
        log_file = tmp_path / "app.log"
        configure_logging(file_path=log_file)
        h = logging.getLogger().handlers[0]
        assert isinstance(h, logging.handlers.RotatingFileHandler)
        # Closing matters on Windows so the temp dir can be cleaned up.
        h.close()

    def test_file_path_writes_records(self, tmp_path):
        log_file = tmp_path / "app.log"
        configure_logging(file_path=log_file, json_format=True)
        logger = logging.getLogger("primer.test")
        logger.info("hello-from-file")
        for h in logging.getLogger().handlers:
            h.flush()
            h.close()
        data = log_file.read_text(encoding="utf-8").strip()
        record = json.loads(data)
        assert record["message"] == "hello-from-file"

    def test_file_path_creates_parent_directory(self, tmp_path):
        log_file = tmp_path / "nested" / "deeper" / "app.log"
        configure_logging(file_path=log_file)
        logging.getLogger("primer.test").info("create-parents")
        for h in logging.getLogger().handlers:
            h.flush()
            h.close()
        assert log_file.exists()


# ============================================================================
# JSON formatter — required fields, extras, exception traceback
# ============================================================================


class TestJsonFormatter:
    def test_required_fields_present(self):
        logger, buf = _capture_to_buf(json_format=True)
        logger.info("hello")
        record = json.loads(buf.getvalue().strip())
        assert record["level"] == "INFO"
        assert record["logger"] == "primer.test"
        assert record["message"] == "hello"
        assert "timestamp" in record

    def test_timestamp_is_iso_8601_utc(self):
        logger, buf = _capture_to_buf(json_format=True)
        logger.info("x")
        record = json.loads(buf.getvalue().strip())
        assert "T" in record["timestamp"]
        assert record["timestamp"].endswith("+00:00")

    def test_extra_fields_propagate(self):
        logger, buf = _capture_to_buf(json_format=True)
        logger.info("hello", extra={"request_id": "req_abc", "model": "gpt-4"})
        record = json.loads(buf.getvalue().strip())
        assert record["request_id"] == "req_abc"
        assert record["model"] == "gpt-4"

    def test_non_colliding_extra_passes_through(self):
        # Sanity: a non-colliding extra key just lands in the payload
        # alongside the reserved fields.
        logger, buf = _capture_to_buf(json_format=True)
        logger.info("hello", extra={"custom": "x"})
        record = json.loads(buf.getvalue().strip())
        assert record["level"] == "INFO"
        assert record["custom"] == "x"

    def test_extra_cannot_overwrite_payload_keys(self):
        # The four payload keys ('timestamp', 'level', 'logger',
        # 'message') must never be silently overwritten by an extra
        # with the same name. 'message' is protected by being in
        # _RESERVED_RECORD_ATTRS; 'level', 'logger', 'timestamp' are
        # protected by the explicit `if key in payload` guard in
        # _JsonFormatter.format.
        logger, buf = _capture_to_buf(json_format=True)
        logger.info(
            "hello",
            extra={
                "level": "HACKED",
                "logger": "evil.module",
                "timestamp": "1970-01-01T00:00:00+00:00",
            },
        )
        record = json.loads(buf.getvalue().strip())
        assert record["level"] == "INFO"
        assert record["logger"] == "primer.test"
        assert record["timestamp"] != "1970-01-01T00:00:00+00:00"
        assert "T" in record["timestamp"]  # real ISO 8601, not the hacked value

    def test_exception_traceback_included(self):
        logger, buf = _capture_to_buf(json_format=True)
        try:
            raise ValueError("bang")
        except ValueError:
            logger.exception("oh no")
        record = json.loads(buf.getvalue().strip())
        assert record["level"] == "ERROR"
        assert "traceback" in record
        assert "ValueError: bang" in record["traceback"]

    def test_no_traceback_key_without_exc_info(self):
        logger, buf = _capture_to_buf(json_format=True)
        logger.info("nothing wrong")
        record = json.loads(buf.getvalue().strip())
        assert "traceback" not in record

    def test_non_serializable_extras_handled_via_default_str(self):
        class Opaque:
            def __repr__(self) -> str:
                return "<Opaque>"

        logger, buf = _capture_to_buf(json_format=True)
        logger.info("x", extra={"thing": Opaque()})
        record = json.loads(buf.getvalue().strip())
        assert record["thing"] == "<Opaque>"


# ============================================================================
# Dev formatter — human-readable single-line output
# ============================================================================


class TestDevFormatter:
    def test_basic_format(self):
        logger, buf = _capture_to_buf(json_format=False)
        logger.info("hello")
        line = buf.getvalue().strip()
        assert "[INFO]" in line
        assert "primer.test" in line
        assert line.endswith("hello")

    def test_warning_level_in_output(self):
        logger, buf = _capture_to_buf(json_format=False)
        logger.warning("careful")
        assert "[WARNING]" in buf.getvalue()

    def test_exception_traceback_inline(self):
        logger, buf = _capture_to_buf(json_format=False)
        try:
            raise RuntimeError("bang")
        except RuntimeError:
            logger.exception("oh no")
        output = buf.getvalue()
        assert "[ERROR]" in output
        assert "RuntimeError: bang" in output
