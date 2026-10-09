"""The log filter masks Bearer and Basic tokens, not only URL credentials (security ticket 01a1201c-8918, item 1; #676 security review).

``_UrlSecretFilter`` applied ``redact_url_secrets`` only, so a tool failure's warning (``extra={"error": str(exc)}``), a ``%s`` that formats an exception,
a ``logger.exception`` traceback or a record whose message IS an exception kept the ``Authorization`` header a library echoed back. It applies
``redact_credentials`` (URL credentials, Bearer and Basic tokens) to every place it already looked at: the message, the args, the string extras and the
cached traceback. Text with no credential comes back as it was.
"""

from __future__ import annotations

import base64
import io
import json
import logging

import pytest

from primer.common.log import configure_logging

BEARER = "sk-bearer-ABCDEFGH12345678"
BASIC = base64.b64encode(b"svc-user:hunter2pw").decode()
HEADER = f"Authorization: Bearer {BEARER}"
BASIC_HEADER = f"Authorization: Basic {BASIC}"


@pytest.fixture(autouse=True)
def _reset_logging():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def _configured(json_format: bool) -> io.StringIO:
    """configure_logging(), then point ITS handler (filters and all) at a buffer: the masking is part of the configured pipeline."""
    configure_logging(json_format=json_format)
    buf = io.StringIO()
    logging.getLogger().handlers[0].setStream(buf)
    return buf


@pytest.fixture(params=[True, False], ids=["json", "dev"])
def stream(request) -> io.StringIO:
    return _configured(request.param)


@pytest.fixture
def json_stream() -> io.StringIO:
    """For what only the JSON formatter prints: the dev formatter writes no extras, so a test of an extra on it could not fail."""
    return _configured(True)


def _last_record(buf: io.StringIO) -> dict:
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def _clean(out: str) -> None:
    assert BEARER not in out, out
    assert BASIC not in out and "hunter2pw" not in out, out


def test_a_bearer_token_in_the_message_is_masked(stream) -> None:
    logging.getLogger("primer.test").warning(f"upstream refused the call ({HEADER}) with 401")

    out = stream.getvalue()
    _clean(out)
    assert "upstream refused the call" in out and "401" in out and "[REDACTED]" in out


def test_a_basic_token_in_the_message_is_masked(stream) -> None:
    logging.getLogger("primer.test").warning(f"upstream refused the call ({BASIC_HEADER}) with 401")

    _clean(stream.getvalue())


def test_a_bearer_token_in_a_percent_s_arg_is_masked(stream) -> None:
    logging.getLogger("primer.test").warning("tool %s failed: %s", "fetch", RuntimeError(f"401 for {HEADER}"))

    out = stream.getvalue()
    _clean(out)
    assert "tool fetch failed" in out


@pytest.mark.parametrize("scheme, token", [("Bearer", BEARER), ("Basic", BASIC)])
def test_a_token_split_across_the_format_string_and_its_args_is_masked(stream, scheme, token) -> None:
    """The scheme is in the format string and the token is the ``%s`` arg: neither half is a credential on its own (the arg is a bare token, the format string
    ends in ``Bearer %s``), so only the formatted message shows one. That is the collapse step's job, and it must apply the Bearer and Basic rules, not the URL
    rules alone."""
    logging.getLogger("primer.test").warning("upstream refused: Authorization: " + scheme + " %s", token)

    out = stream.getvalue()
    _clean(out)
    assert "upstream refused: Authorization: " + scheme in out and "[REDACTED]" in out


def test_a_token_in_a_string_extra_is_masked(json_stream) -> None:
    """The tool-failure warnings of the tool manager log ``extra={"error": str(exc)}``."""
    logging.getLogger("primer.test").warning("tool call failed", extra={"error": f"401 {HEADER} {BASIC_HEADER}"})

    _clean(json_stream.getvalue())
    assert "401" in _last_record(json_stream)["error"]


@pytest.mark.parametrize(
    "make",
    [
        lambda: {"error": f"401 {HEADER}", "attempts": [f"retry with {BASIC_HEADER}", "plain"]},
        lambda: [f"401 {HEADER}", 7],
        lambda: RuntimeError(f"401 {HEADER}"),
    ],
    ids=["dict", "list", "exception"],
)
def test_a_token_in_a_non_string_extra_is_masked(json_stream, make) -> None:
    """The formatter prints an extra that is not a string (a dict, a list, an exception object) through ``json.dumps(default=str)``, after the filter's
    string-only pass: the formatter masks the finished line."""
    logging.getLogger("primer.test").warning("tool call failed", extra={"detail": make()})

    _clean(json_stream.getvalue())
    assert "detail" in _last_record(json_stream)


def test_a_token_in_a_logged_traceback_is_masked(stream) -> None:
    try:
        raise RuntimeError(f"401 Unauthorized, request carried {HEADER}")
    except RuntimeError:
        logging.getLogger("primer.test").exception("resume hook failed")

    out = stream.getvalue()
    _clean(out)
    assert "resume hook failed" in out and "401 Unauthorized" in out


def test_a_record_whose_message_is_an_exception_is_masked(stream) -> None:
    logging.getLogger("primer.test").warning(RuntimeError(f"401 for {HEADER}"))

    _clean(stream.getvalue())


def test_a_url_credential_is_still_masked(stream) -> None:
    logging.getLogger("primer.test").warning("fetch failed: https://u:pw-secret@h.example/v1?api_key=KEY-SECRET-1")

    out = stream.getvalue()
    assert "pw-secret" not in out and "KEY-SECRET-1" not in out


@pytest.mark.parametrize("text", ["Basic authentication is required for this endpoint", "bearer of bad news", "plain words, a number 12345"])
def test_ordinary_words_are_not_masked(stream, text) -> None:
    logging.getLogger("primer.test").warning(text)

    assert text in stream.getvalue()


def test_the_masked_json_line_stays_valid_and_loses_only_the_secret(json_stream) -> None:
    """The finished line is masked as text, so a rule must stop at the end of the JSON string it is in: the closing quote after a token, and the ``\\uXXXX``
    escapes json.dumps writes inside a secret, must not make the line invalid or leave a tail of the secret."""
    logging.getLogger("primer.test").warning(
        "refused",
        extra={
            "quoted": f'"{HEADER}"',
            "nested": {"headers": [HEADER, "plain"], "query": "GET /v1/x?api_key=s\u00e9cr\u00e8t-VALUE&page=2 -> 401"},
        },
    )

    record = _last_record(json_stream)
    assert record["quoted"] == '"Authorization: Bearer [REDACTED]"'
    assert record["nested"] == {
        "headers": ["Authorization: Bearer [REDACTED]", "plain"],
        "query": "GET /v1/x?api_key=[REDACTED]&page=2 -> 401",
    }
