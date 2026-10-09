"""An ERROR record never stores a URL credential (security ticket 01a11f35-ad20).

``messages.jsonl`` is served whole by ``GET /v1/sessions/{sid}/messages`` and read by every console. The adapters build the message of an in-stream
EXCEPTION through ``describe_failure`` (which masks the provider's configured key, URL-borne credentials, Bearer and Basic tokens), but a stream
``Error`` EVENT reached the record untouched: an OpenResponses ``error`` event carries the provider's own text, and any other source of an
``Error`` or a graph node's failure text may carry a URL with userinfo or a ``?api_key=`` query. The record writer now redacts URL credentials
(``primer.common.log.redact_url_secrets``, the same function dispatch's own ERROR record and the turn log already use), whatever produced the event.
The adapter's own masking of an OpenResponses ``error`` event is tested in ``tests/llm/test_openresponses_error_event_text.py``.
"""

from __future__ import annotations

import pytest

from primer.graph.base import _GraphErrorEvent
from primer.model.chat import Error
from primer.session.persistence import _CoalesceState, translate_stream_event

LEAKY = "upstream refused https://svc-user:hunter2pw@gateway.internal/v1/chat?api_key=SKSECRET123456&x=1 (try again)"


def _assert_clean(text: str) -> None:
    assert "hunter2pw" not in text and "SKSECRET123456" not in text, text
    assert "gateway.internal" in text and "try again" in text, "the rest of the message must survive"


@pytest.mark.parametrize("fatal", [True, False])
def test_a_stream_error_event_is_recorded_without_url_credentials(fatal):
    record = translate_stream_event(Error(code="server_error", message=LEAKY, fatal=fatal), _CoalesceState())
    record = record[0] if isinstance(record, list) else record

    _assert_clean(record.payload["message"])
    assert record.payload["code"] == "server_error" and record.payload["fatal"] is fatal


def test_a_graph_nodes_failure_text_is_recorded_without_url_credentials():
    record = translate_stream_event(_GraphErrorEvent(code="node_failed", message=LEAKY, node_id="n1", path=None), _CoalesceState())
    record = record[0] if isinstance(record, list) else record

    _assert_clean(record.payload["message"])


def test_an_error_without_credentials_is_recorded_as_it_was():
    record = translate_stream_event(Error(code="x", message="plain words, a number 12345 and https://example.com/a?b=c", fatal=True), _CoalesceState())
    record = record[0] if isinstance(record, list) else record

    assert record.payload["message"] == "plain words, a number 12345 and https://example.com/a?b=c"
