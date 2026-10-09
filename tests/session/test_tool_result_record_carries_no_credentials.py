"""A failed tool call's TOOL_RESULT record carries no credential (security ticket 01a11fbc-d6de).

``messages.jsonl`` is served whole by ``GET /v1/sessions/{sid}/messages``. A tool's failure text is whatever its exception printed: httpx prints the
request URL whole (userinfo and ``?api_key=`` included), a child agent's or graph's failure body is interpolated into the parent's tool result, and
the record writer stored it all raw. The writer now redacts the text of a result that is an ERROR (URL credentials, Bearer and Basic tokens). A
successful result is DATA (a fetched page, a file) and is not touched: a URL with userinfo in a file the operator asked for is the operator's.
"""

from __future__ import annotations

from primer.model.chat import ExtendedEvent, _ExecutorToolResult
from primer.session.persistence import _CoalesceState, translate_stream_event

LEAKY = (
    "ConnectError: All connection attempts failed for url 'https://svc-user:hunter2pw@gateway.internal/v1/x?api_key=SKSECRET123456' "
    "(retry with Bearer sk-abcdefgh12345678 or Basic dXNlcjpwYXNzd29yZA==)"
)
SECRETS = ("hunter2pw", "SKSECRET123456", "sk-abcdefgh12345678", "dXNlcjpwYXNzd29yZA==")


def _record(output: str, *, error: bool):
    record = translate_stream_event(
        ExtendedEvent(extended=_ExecutorToolResult(call_id="call_0", output=output, error=error)), _CoalesceState(),
    )
    return record[0] if isinstance(record, list) else record


def test_an_error_result_is_recorded_without_the_credentials_in_its_text():
    out = _record(LEAKY, error=True).payload["output"]

    for secret in SECRETS:
        assert secret not in out, f"{secret!r} leaked in {out!r}"
    assert "gateway.internal" in out and "All connection attempts failed" in out, "the rest of the message must survive"
    assert _record(LEAKY, error=True).payload["error"] is True


def test_an_error_result_without_a_credential_is_recorded_as_it_was():
    plain = "ConnectError: All connection attempts failed for url 'https://example.com/v1/x?page=2' (12345, Basic authentication is required)"

    assert _record(plain, error=True).payload["output"] == plain


def test_a_successful_result_is_data_and_is_recorded_as_it_was():
    """A fetched page or a file may hold a URL with userinfo or a token on purpose; only a failure's text is a leak."""
    assert _record(LEAKY, error=False).payload["output"] == LEAKY
