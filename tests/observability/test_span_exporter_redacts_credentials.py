"""No span leaves the process with a credential in it (security ticket 01a12171-2d5a; #697 security review).

``tracing.span`` masks the failures of Primer's own spans, but the spans the AUTO-instrumentors create are exported as they are when ``otlp_endpoint`` is set:

* the httpx CLIENT spans keep ``/bot<id>:<secret>/`` (every python-telegram-bot call and ``getUpdates`` poll), ``?key=`` and ``?api_key=`` in ``http.url`` (OTel
  strips only userinfo and the AWS / Google signature parameters);
* the FastAPI SERVER spans keep ``/v1/webhooks/<token>`` and the query string in ``http.target`` / ``http.url``;
* an exception that escapes a route is recorded raw on the server span (message and a stacktrace with its causes), and a default-recording parent re-records
  an exception ``tracing.span`` had masked.

``RedactingSpanExporter`` wraps the OTLP exporter and passes every string a finished span carries (name, attributes, event and link attributes, status
description) through ``redact_credentials`` before it is exported, so all three are closed at the one place every span crosses. ``install_log_correlation``
no longer asks the logging instrumentor for the OTel ``LoggingHandler`` (it reads the raw ``exc_info``).
"""

from __future__ import annotations

import http.server
import logging
import threading
import warnings
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

BOT = "123456789:AAHsecretBotTokenValue_abcdefghij"
KEY = "SKSECRETKEY123456"
WEBHOOK = "whk_live_abcdef0123456789abcdef"
BEARER = "sk-bearer-ABCDEFGH12345678"
HEADER = f"Authorization: Bearer {BEARER}"
SECRETS = (BOT, KEY, WEBHOOK, BEARER, "hunter2pw")


def _redacting(inner: SpanExporter) -> SpanExporter:
    from primer.observability.span_redaction import RedactingSpanExporter

    return RedactingSpanExporter(inner)


@pytest.fixture
def pipeline() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_redacting(inner)))
    yield provider, inner
    provider.shutdown()


def _everything_exported(exporter: InMemorySpanExporter) -> str:
    """Every string a collector would receive for the finished spans: names, attributes, events with their attributes, links and the status description."""
    out: list[str] = []
    for span in exporter.get_finished_spans():
        out.append(span.name)
        out.append(repr(dict(span.attributes or {})))
        out.append(str(span.status.description))
        for event in span.events:
            out.append(event.name)
            out.append(repr(dict(event.attributes or {})))
        for link in span.links:
            out.append(repr(dict(link.attributes or {})))
    return "\n".join(out)


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} was exported: {text!r}"


# ---- the exporter ---------------------------------------------------------------------------------------------------------------------------------


def test_every_string_a_span_carries_is_masked(pipeline) -> None:
    provider, exporter = pipeline
    tracer = provider.get_tracer("t")
    linked = tracer.start_span("linked").get_span_context()

    with tracer.start_as_current_span(f"GET /v1/webhooks/{WEBHOOK}", links=[Link(linked, {"url": f"https://svc:hunter2pw@h/x?api_key={KEY}"})]) as span:
        span.set_attribute("http.url", f"https://api.telegram.org/bot{BOT}/getMe?key={KEY}")
        span.set_attribute("http.target", f"/v1/webhooks/{WEBHOOK}?token={KEY}")
        span.set_attribute("urls", [f"https://h/?key={KEY}", "plain"])
        span.add_event("exception", {"exception.message": f"401 {HEADER}", "exception.stacktrace": f"Traceback (most recent call last):\nRuntimeError: {HEADER}"})
        span.set_status(Status(StatusCode.ERROR, f"ConnectError: https://svc:hunter2pw@h/?api_key={KEY}"))

    text = _everything_exported(exporter)
    _assert_clean(text)
    [done] = [s for s in exporter.get_finished_spans() if s.name != "linked"]
    assert done.attributes["http.url"] == "https://api.telegram.org/bot[REDACTED]/getMe?key=[REDACTED]"
    assert done.attributes["urls"] == ("https://h/?key=[REDACTED]", "plain")
    assert done.status.status_code == StatusCode.ERROR and "ConnectError" in done.status.description
    assert [e.name for e in done.events] == ["exception"], "the event itself is kept"


def test_the_query_string_attribute_is_masked_though_it_has_no_leading_question_mark(pipeline) -> None:
    """``url.query`` (current semantic conventions) is ``api_key=...&page=2``: the first parameter has no ``?`` before it."""
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("url.query", f"api_key={KEY}&page=2")
        span.set_attribute("http.query", f"token={KEY}&page=2")

    [done] = exporter.get_finished_spans()
    assert done.attributes["url.query"] == "api_key=[REDACTED]&page=2"
    assert done.attributes["http.query"] == "token=[REDACTED]&page=2"


def test_a_captured_credential_header_is_masked_whole(pipeline) -> None:
    """With header capture on, a bare ``x-api-key`` value has no URL or Bearer shape to recognise: the header's NAME says it is a secret."""
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("http.request.header.x_api_key", [KEY])
        span.set_attribute("http.request.header.cookie", ["session=abc123"])
        span.set_attribute("http.request.header.accept", ["application/json"])

    [done] = exporter.get_finished_spans()
    assert done.attributes["http.request.header.x_api_key"] == ("[REDACTED]",)
    assert done.attributes["http.request.header.cookie"] == ("[REDACTED]",)
    assert done.attributes["http.request.header.accept"] == ("application/json",), "an ordinary header is untouched"


def test_a_span_with_no_credential_is_exported_as_it_was() -> None:
    bare, masked = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(bare))
    provider.add_span_processor(SimpleSpanProcessor(_redacting(masked)))
    tracer = provider.get_tracer("t", "1.2")

    with tracer.start_as_current_span("outer") as outer:
        outer.set_attribute("tool.name", "fetch")
        outer.set_attribute("llm.usage.tokens_in", 12)
        outer.set_attribute("ratio", 0.5)
        outer.set_attribute("ok", True)
        outer.add_event("note", {"detail": "plain words"})
        with tracer.start_as_current_span("inner"):
            pass
        outer.set_status(Status(StatusCode.OK))

    def fields(s):
        return (s.name, dict(s.attributes), [(e.name, dict(e.attributes), e.timestamp) for e in s.events], s.kind, s.status.status_code, s.status.description,
                s.start_time, s.end_time, s.context.trace_id, s.context.span_id, s.parent.span_id if s.parent else None,
                dict(s.resource.attributes), s.instrumentation_scope.name, s.instrumentation_scope.version)

    assert [fields(s) for s in masked.get_finished_spans()] == [fields(s) for s in bare.get_finished_spans()]
    provider.shutdown()


def test_the_dropped_counts_survive() -> None:
    from opentelemetry.sdk.trace import SpanLimits

    inner = InMemorySpanExporter()
    limited = TracerProvider(span_limits=SpanLimits(max_attributes=1))
    limited.add_span_processor(SimpleSpanProcessor(_redacting(inner)))

    with limited.get_tracer("t").start_as_current_span("s") as span:
        span.set_attribute("a", "1")
        span.set_attribute("b", "2")
        span.set_attribute("c", "3")

    [done] = inner.get_finished_spans()
    assert done.dropped_attributes == 2, "what the SDK dropped is still reported as dropped"
    limited.shutdown()


def test_a_redaction_that_fails_masks_the_value_and_still_exports_the_span(pipeline, monkeypatch) -> None:
    import primer.observability.span_redaction as module

    provider, exporter = pipeline
    real = module.redact_credentials

    def flaky(text: str) -> str:
        if "boom" in text:
            raise RuntimeError("the masker failed")
        return real(text)

    monkeypatch.setattr(module, "redact_credentials", flaky)

    with provider.get_tracer("t").start_as_current_span("s") as span:
        span.set_attribute("bad", "boom")
        span.set_attribute("good", f"?key={KEY}")

    [done] = exporter.get_finished_spans()
    assert done.attributes["bad"] == "[REDACTED]", "a value that cannot be checked is not exported"
    assert done.attributes["good"] == "?key=[REDACTED]"


class _Recording(SpanExporter):
    def __init__(self, result: SpanExportResult = SpanExportResult.SUCCESS) -> None:
        self.result, self.calls, self.flushed, self.shut = result, [], [], 0

    def export(self, spans):
        self.calls.append(list(spans))
        return self.result

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flushed.append(timeout_millis)
        return True

    def shutdown(self) -> None:
        self.shut += 1


def test_the_wrapper_passes_the_inner_exporters_result_and_lifecycle_through() -> None:
    inner = _Recording(SpanExportResult.FAILURE)
    wrapper = _redacting(inner)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(wrapper))
    with provider.get_tracer("t").start_as_current_span("s"):
        pass

    assert len(inner.calls) == 1 and wrapper.export(inner.calls[0]) is SpanExportResult.FAILURE
    assert wrapper.force_flush(1234) is True and inner.flushed == [1234]
    wrapper.shutdown()
    assert inner.shut == 1


# ---- round 2 (#703 review): default-deny headers, more query names, bytes, the original span -------------------------------------------------------

SAFE_HEADERS = [
    "accept", "accept_encoding", "accept_language", "cache_control", "connection", "content_encoding", "content_length", "content_type", "host",
    "traceparent", "tracestate", "user_agent", "x_request_id",
]
UNSAFE_HEADERS = [
    "x_goog_api_key", "x_telegram_bot_api_secret_token", "x_amz_security_token", "ocp_apim_subscription_key", "x_subscription_token", "private_token",
    "x_primer_signature", "x_my_mcp_custom_header", "authorization", "cookie", "x_api_key",
]


def test_a_captured_header_is_masked_unless_it_is_on_the_safe_list(pipeline) -> None:
    """Default-deny: a header Primer sends itself (Gemini's ``x-goog-api-key``, an operator-named MCP header) has no shape to recognise, so every captured header
    value is masked except a short list of headers that never carry a secret."""
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        for name in SAFE_HEADERS + UNSAFE_HEADERS:
            span.set_attribute(f"http.request.header.{name}", ["S3CRETvalue-" + name])
            span.set_attribute(f"http.response.header.{name}", ["S3CRETvalue-" + name])

    [done] = exporter.get_finished_spans()
    for direction in ("request", "response"):
        for name in SAFE_HEADERS:
            assert done.attributes[f"http.{direction}.header.{name}"] == ("S3CRETvalue-" + name,), f"{direction} header {name} is on the safe list"
        for name in UNSAFE_HEADERS:
            assert done.attributes[f"http.{direction}.header.{name}"] == ("[REDACTED]",), f"{direction} header {name} was exported"


def test_a_header_name_is_normalised_before_it_is_judged(pipeline) -> None:
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("http.request.header.Content-Type", ["application/json"])
        span.set_attribute("http.request.header.x-goog-api-key", ["AIzaSECRET"])
        span.set_attribute("http.request.header.X_REQUEST_ID", ["r-1"])

    [done] = exporter.get_finished_spans()
    assert done.attributes["http.request.header.Content-Type"] == ("application/json",)
    assert done.attributes["http.request.header.x-goog-api-key"] == ("[REDACTED]",)
    assert done.attributes["http.request.header.X_REQUEST_ID"] == ("r-1",)


@pytest.mark.asyncio
async def test_a_header_primer_sends_and_a_set_cookie_are_not_exported_through_the_real_httpx_instrumentor(pipeline, local_server, monkeypatch) -> None:
    """Header capture is opt-in (``OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*``, read when the instrumentor is installed). With it on, the CLIENT span of the real
    instrumentor carries the request headers Primer sends (``x-goog-api-key``, an MCP-style custom header) and the response's ``set-cookie``."""
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST", ".*")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE", ".*")
    provider, exporter = pipeline
    instrumentor = HTTPXClientInstrumentor()
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{local_server}/x", headers={"x-goog-api-key": "AIzaGEMINIsecret123", "x-mcp-tenant-token": "MCPcustomSECRET456"},
            )
        assert response.status_code == 200
    finally:
        instrumentor.uninstrument()

    spans = exporter.get_finished_spans()
    text = _everything_exported(exporter)
    assert "AIzaGEMINIsecret123" not in text and "MCPcustomSECRET456" not in text and "SETCOOKIEsecret789" not in text, text
    captured = {k: v for s in spans for k, v in s.attributes.items() if k.startswith("http.request.header.") or k.startswith("http.response.header.")}
    assert captured, "header capture was on: the headers were recorded, and masked"
    assert captured.get("http.request.header.x_goog_api_key") == ("[REDACTED]",)
    assert captured.get("http.response.header.set_cookie") == ("[REDACTED]",)


SPAN_QUERY_SECRETS = [
    "signature", "sig", "code", "auth_token", "accessToken", "access_token", "private_token", "jwt", "passwd", "hm",
    "X-Amz-Security-Token", "X-Amz-Signature", "X-Amz-Credential", "x-amz-signature", "hub.verify_token", "subscription-key", "Signature", "CODE",
]


@pytest.mark.parametrize("name", SPAN_QUERY_SECRETS)
def test_a_query_value_under_a_credential_name_redact_credentials_does_not_list_is_masked(pipeline, name) -> None:
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("http.url", f"https://h.example/x?page=2&{name}=S3CRET-value_9&state=ok")
        span.set_attribute("http.target", f"/x?{name}=S3CRET-value_9")
        span.set_attribute("url.query", f"{name}=S3CRET-value_9&page=2")

    [done] = exporter.get_finished_spans()
    assert done.attributes["http.url"] == f"https://h.example/x?page=2&{name}=[REDACTED]&state=ok", "the name stays, the value goes, the rest is intact"
    assert done.attributes["http.target"] == f"/x?{name}=[REDACTED]"
    assert done.attributes["url.query"] == f"{name}=[REDACTED]&page=2"


@pytest.mark.parametrize("param", ["page", "q", "state", "limit", "codec", "message_id"])
def test_an_ordinary_query_parameter_is_left_alone(pipeline, param) -> None:
    """``codec`` and ``message_id`` contain a credential name as a prefix or a part: only the NAME is matched, whole."""
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("http.url", f"https://h.example/x?{param}=value123")

    [done] = exporter.get_finished_spans()
    assert done.attributes["http.url"] == f"https://h.example/x?{param}=value123"


def test_a_percent_encoded_bot_token_is_masked(pipeline) -> None:
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("http.url", "https://api.telegram.org/bot123456789%3AAAHsecretBotTokenValue_abc/getMe")

    [done] = exporter.get_finished_spans()
    assert done.attributes["http.url"] == "https://api.telegram.org/bot[REDACTED]/getMe"


@pytest.mark.asyncio
async def test_a_presigned_url_and_an_oauth_code_are_masked_through_the_real_httpx_instrumentor(pipeline, local_server) -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    provider, exporter = pipeline
    instrumentor = HTTPXClientInstrumentor()
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider)
    try:
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{local_server}/cb?code=OAUTHcode123456&X-Amz-Security-Token=STStoken789&X-Amz-Signature=SIGsecret000&state=ok")
    finally:
        instrumentor.uninstrument()

    text = _everything_exported(exporter)
    for secret in ("OAUTHcode123456", "STStoken789", "SIGsecret000"):
        assert secret not in text, text
    assert "state=ok" in text


def test_a_bytes_attribute_is_masked_and_re_encoded_only_when_it_changed(pipeline) -> None:
    provider, exporter = pipeline
    clean = b"plain bytes, nothing to hide"
    invalid = b"\xff\xfe not utf-8 \x80"

    with provider.get_tracer("t").start_as_current_span("s") as span:
        span.set_attribute("leaky", b"https://h.example/?key=SECRETKEY123")
        span.set_attribute("clean", clean)
        span.set_attribute("invalid", invalid)

    [done] = exporter.get_finished_spans()
    assert done.attributes["leaky"] == b"https://h.example/?key=[REDACTED]"
    assert done.attributes["clean"] is clean or done.attributes["clean"] == clean
    assert done.attributes["invalid"] == invalid, "bytes that are not text, with nothing to mask, are exported as they were"


def test_the_redaction_marker_of_a_bare_query_has_no_stray_bracket(pipeline) -> None:
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("s") as span:
        span.set_attribute("url.query", f"key={KEY}")

    [done] = exporter.get_finished_spans()
    assert done.attributes["url.query"] == "key=[REDACTED]"


# ---- the original span is exported when nothing changed; a rebuilt one keeps every count and field ----------------------------------------------


class _Capturing(SpanExporter):
    def __init__(self) -> None:
        self.spans: list = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def test_a_span_with_nothing_to_mask_is_exported_as_the_original_object() -> None:
    capture = _Capturing()
    bare = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_event_attributes=1, max_link_attributes=1))
    provider.add_span_processor(SimpleSpanProcessor(bare))
    provider.add_span_processor(SimpleSpanProcessor(_redacting(capture)))
    tracer = provider.get_tracer("t")
    linked = tracer.start_span("linked").get_span_context()

    with tracer.start_as_current_span("clean", links=[Link(linked, {"a": "1", "b": "2", "c": "3"})]) as span:
        span.set_attribute("tool.name", "fetch")
        span.add_event("note", {"x": "1", "y": "2", "z": "3"})

    [original] = [s for s in bare.get_finished_spans() if s.name == "clean"]
    [exported] = [s for s in capture.spans if s.name == "clean"]
    assert exported is original, "nothing to mask: the span the SDK made is the span that is exported"
    assert exported.events[0].dropped_attributes == 2 and exported.links[0].dropped_attributes == 2
    provider.shutdown()


def test_a_rebuilt_span_keeps_its_dropped_counts_for_events_and_links_too() -> None:
    capture = _Capturing()
    provider = TracerProvider(span_limits=SpanLimits(max_attributes=1, max_events=1, max_links=1, max_event_attributes=1, max_link_attributes=1))
    provider.add_span_processor(SimpleSpanProcessor(_redacting(capture)))
    tracer = provider.get_tracer("t")
    first = tracer.start_span("first").get_span_context()
    second = tracer.start_span("second").get_span_context()

    # The limits keep the LAST event / link / attribute, so the ones that must be examined are added last.
    with tracer.start_as_current_span("leaky", links=[Link(second, {}), Link(first, {"a": "1", "b": "2", "key": f"https://h/?key={KEY}"})]) as span:
        span.set_attribute("dropped1", "x")
        span.set_attribute("dropped2", "y")
        span.set_attribute("http.url", f"https://h/?key={KEY}")                    # the limit keeps the LAST attribute set
        span.add_event("dropped-event", {})
        span.add_event("kept", {"v": "2", "w": "3", "u": f"https://h/?key={KEY}"})

    [rebuilt] = [s for s in capture.spans if s.name == "leaky"]
    assert rebuilt.attributes["http.url"] == "https://h/?key=[REDACTED]", "the span was rebuilt"
    assert (rebuilt.dropped_attributes, rebuilt.dropped_events, rebuilt.dropped_links) == (2, 1, 1)
    assert rebuilt.events[0].dropped_attributes == 2 and rebuilt.links[0].dropped_attributes == 2
    provider.shutdown()


def test_a_rebuilt_span_keeps_its_kind_resource_scope_and_trace_state() -> None:
    capture = _Capturing()
    resource = Resource.create({"service.name": "primer-test", "deployment.environment": "ci"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(_redacting(capture)))
    bare = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(bare))

    with provider.get_tracer("client-lib", "9.9").start_as_current_span("GET", kind=SpanKind.CLIENT) as span:
        span.set_attribute("http.url", f"https://h/?key={KEY}")

    [rebuilt] = capture.spans
    [original] = bare.get_finished_spans()
    assert rebuilt is not original and rebuilt.attributes["http.url"] == "https://h/?key=[REDACTED]"
    assert rebuilt.kind == SpanKind.CLIENT
    assert dict(rebuilt.resource.attributes) == dict(resource.attributes)
    assert (rebuilt.instrumentation_scope.name, rebuilt.instrumentation_scope.version) == ("client-lib", "9.9")
    assert rebuilt.context.trace_id == original.context.trace_id and rebuilt.context.span_id == original.context.span_id
    assert rebuilt.context.trace_state == original.context.trace_state and rebuilt.parent == original.parent
    assert (rebuilt.start_time, rebuilt.end_time) == (original.start_time, original.end_time)
    provider.shutdown()


def test_a_rebuilt_span_serialises_masked_and_exporting_it_warns_of_nothing() -> None:
    """``to_json`` (the console exporter) reads the span's private fields: the rebuilt span must be masked there too. And no deprecated SDK argument is used."""
    capture = _Capturing()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_redacting(capture)))

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with provider.get_tracer("t").start_as_current_span("GET") as span:
            span.set_attribute("http.url", f"https://h/?key={KEY}")
            span.add_event("e", {"m": f"401 {HEADER}"})

    [rebuilt] = capture.spans
    dumped = rebuilt.to_json()
    assert KEY not in dumped and BEARER not in dumped and "[REDACTED]" in dumped
    provider.shutdown()


# ---- round 3 (#703 re-review): the fragment, more credential names, ordinary names left alone, isolation per span ----------------------------------

MORE_SPAN_QUERY_SECRETS = [
    "access_token", "accessToken", "access-token", "api_token", "apiToken", "access_key", "accessKey", "oauth_token", "session_token", "sessionToken",
    "id_token", "id_token_hint", "auth_token", "auth-token", "x-goog-signature", "X-Goog-Credential", "x-goog-algorithm",
]


@pytest.mark.parametrize("name", MORE_SPAN_QUERY_SECRETS)
def test_more_credential_names_are_masked_in_a_query_and_as_the_first_parameter_of_a_fragment(pipeline, name) -> None:
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("http.url", f"https://h.example/x?page=2&{name}=S3CRET-value_9&state=ok")
        span.set_attribute("http.redirect", f"https://h.example/cb#{name}=S3CRET-value_9&state=ok")
        span.set_attribute("free.text", f"redirected to https://h.example/cb#state=ok&{name}=S3CRET-value_9 and back")

    [done] = exporter.get_finished_spans()
    assert done.attributes["http.url"] == f"https://h.example/x?page=2&{name}=[REDACTED]&state=ok"
    assert done.attributes["http.redirect"] == f"https://h.example/cb#{name}=[REDACTED]&state=ok", "a token that is the first fragment parameter"
    assert done.attributes["free.text"] == f"redirected to https://h.example/cb#state=ok&{name}=[REDACTED] and back"


def test_an_ordinary_parameter_that_ends_in_a_credential_name_stays_intact(pipeline) -> None:
    """``zipcode`` ends in ``code``, ``country_code`` ends in ``code``, ``msig`` ends in ``sig``: only the WHOLE name after ``?``, ``&`` or ``#`` is a credential."""
    provider, exporter = pipeline

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("http.url", "https://h.example/x?zipcode=94107&country_code=US&msig=1&sigma=2&tokenizer=x&keyboard=y#anchor_code=3")

    [done] = exporter.get_finished_spans()
    assert done.attributes["http.url"] == "https://h.example/x?zipcode=94107&country_code=US&msig=1&sigma=2&tokenizer=x&keyboard=y#anchor_code=3"


def test_a_masker_that_fails_on_a_bare_query_exports_the_marker(pipeline, monkeypatch) -> None:
    import primer.observability.span_redaction as module

    provider, exporter = pipeline

    def broken(_text: str) -> str:
        raise RuntimeError("the masker failed")

    monkeypatch.setattr(module, "redact_credentials", broken)

    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("url.query", f"page=2&key={KEY}")

    [done] = exporter.get_finished_spans()
    assert done.attributes["url.query"] == "[REDACTED]"


def test_a_span_whose_only_secret_is_a_link_attribute_is_rebuilt_with_the_link_masked() -> None:
    capture = _Capturing()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_redacting(capture)))
    tracer = provider.get_tracer("t")
    linked = tracer.start_span("linked").get_span_context()

    with tracer.start_as_current_span("clean", links=[Link(linked, {"callback": f"https://h/?key={KEY}", "n": 1})]) as span:
        span.set_attribute("tool.name", "fetch")
        span.add_event("note", {"detail": "plain"})

    [exported] = [s for s in capture.spans if s.name == "clean"]
    assert exported.links[0].attributes["callback"] == "https://h/?key=[REDACTED]" and exported.links[0].attributes["n"] == 1
    assert exported.attributes["tool.name"] == "fetch" and exported.events[0].attributes["detail"] == "plain"
    provider.shutdown()


def test_an_event_named_with_a_url_credential_over_clean_attributes_is_masked() -> None:
    capture = _Capturing()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_redacting(capture)))

    with provider.get_tracer("t").start_as_current_span("clean") as span:
        span.add_event(f"GET https://h.example/x?api_key={KEY}", {"status": 200})

    [exported] = capture.spans
    assert exported.events[0].name == "GET https://h.example/x?api_key=[REDACTED]" and exported.events[0].attributes["status"] == 200
    provider.shutdown()


def test_a_span_that_cannot_be_masked_drops_only_itself(monkeypatch, caplog) -> None:
    """One span the masker cannot handle must not cost the up-to-512 spans batched with it: it is dropped, logged, and the rest are exported."""
    import primer.observability.span_redaction as module

    capture = _Capturing()
    real = module.redact_span

    def flaky(span):
        if span.name == "boom":
            raise RuntimeError("cannot mask this one")
        return real(span)

    monkeypatch.setattr(module, "redact_span", flaky)
    provider = TracerProvider()
    exporter = _redacting(capture)
    tracer = provider.get_tracer("t")
    spans = []
    bare = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(bare))
    for name in ("first", "boom", "last"):
        with tracer.start_as_current_span(name):
            pass
    spans = list(bare.get_finished_spans())

    with caplog.at_level(logging.ERROR):
        result = exporter.export(spans)

    assert result is SpanExportResult.SUCCESS
    assert [s.name for s in capture.spans] == ["first", "last"]
    assert any("dropped" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    provider.shutdown()


# ---- setup() wires it ---------------------------------------------------------------------------------------------------------------------------


def test_setup_wraps_the_otlp_exporter(monkeypatch) -> None:
    """What the OTLP exporter RECEIVES is masked: ``setup`` attaches the redacting wrapper, not the bare exporter."""
    import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as otlp

    from primer.api.config import ObservabilityConfig
    from primer.observability import tracing

    received = _Recording()
    monkeypatch.setattr(otlp, "OTLPSpanExporter", lambda **_kw: received)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda _provider: None)
    monkeypatch.setattr(tracing, "_install_auto_instrumentors", lambda: None)
    monkeypatch.setattr(tracing, "_provider", None)

    tracing.setup(ObservabilityConfig(otlp_endpoint="http://collector:4317"))
    provider = tracing._provider
    assert provider is not None
    with provider.get_tracer("t").start_as_current_span("GET") as span:
        span.set_attribute("http.url", f"https://api.telegram.org/bot{BOT}/getMe?key={KEY}")
    provider.force_flush()
    provider.shutdown()

    exported = [s for batch in received.calls for s in batch]
    assert exported, "the span reached the exporter"
    _assert_clean(repr([dict(s.attributes) for s in exported]))


# ---- the auto-instrumentors, end to end ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def local_server() -> Iterator[int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Set-Cookie", "session=SETCOOKIEsecret789")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_an_httpx_call_to_a_bot_token_url_exports_no_token(pipeline, local_server) -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    provider, exporter = pipeline
    instrumentor = HTTPXClientInstrumentor()
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{local_server}/bot{BOT}/getMe?key={KEY}&api_key={KEY}")
        assert response.status_code == 200
    finally:
        instrumentor.uninstrument()

    spans = exporter.get_finished_spans()
    assert spans, "the client span was recorded"
    _assert_clean(_everything_exported(exporter))
    assert any("/getMe" in str(s.attributes) for s in spans), "what was called is still said"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/webhooks/{token}")
    async def hook(token: str) -> dict:
        return {"ok": True}

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError(f"upstream said {HEADER} for https://svc:hunter2pw@h.example/v1?api_key={KEY}")

    return app


@pytest.mark.asyncio
async def test_a_webhook_route_call_and_an_unhandled_route_exception_export_no_secret(pipeline) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    provider, exporter = pipeline
    app = _app()
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            hook = await client.get(f"/v1/webhooks/{WEBHOOK}?token={KEY}")
            boom = await client.get("/boom")
        assert hook.status_code == 200 and boom.status_code == 500
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

    text = _everything_exported(exporter)
    _assert_clean(text)
    assert "/v1/webhooks/" in text, "which route was called is still said"
    assert "RuntimeError" in text, "and that an exception left a route"


# ---- the logging instrumentor ----------------------------------------------------------------------------------------------------------------------


def test_log_correlation_does_not_attach_the_otel_logging_handler(monkeypatch) -> None:
    """The handler the logging instrumentor attaches by default reads each record's raw ``exc_info`` and ships it as a log record: the correlation hook needs
    none of that."""
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    from primer.observability.logging_integration import install_log_correlation

    seen: dict = {}
    monkeypatch.setattr(LoggingInstrumentor, "instrument", lambda _self, **kwargs: seen.update(kwargs))

    install_log_correlation()

    assert seen.get("enable_log_auto_instrumentation") is False
    assert seen.get("set_logging_format") is False and callable(seen.get("log_hook")), "the correlation hook is still installed"
