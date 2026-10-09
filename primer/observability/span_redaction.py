"""A span exporter that masks the credential shapes listed below in a span's name, attributes, events, links and status (security ticket 01a12171-2d5a).

``primer.observability.tracing.span`` masks the failures of Primer's own spans. The spans the AUTO-instrumentors create are a different path: the httpx client
instrumentor puts the whole request URL in ``http.url`` (a Telegram call is ``/bot<id>:<secret>/getMe``, a provider call may carry ``?key=``), a server
instrumentor puts the request path and query in ``http.target`` (a webhook is ``/v1/webhooks/<token>``), and an exception that leaves an instrumented route is
recorded on the server span by the SDK itself (message and a stacktrace with its causes). OTel's own URL redaction strips only userinfo and the AWS / Google
signature parameters. With header capture on (``OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*``) the instrumentors also record request and response headers.

:class:`RedactingSpanExporter` wraps the real exporter, so every finished span crosses it once, whichever code created the span:

* its name, attributes (a sequence or a mapping element by element, ``bytes`` as text), event and link attributes and status description pass through
  :func:`primer.common.log.redact_credentials` (URL credentials, Bearer and Basic tokens) and the query-name rule below;
* a captured header is masked unless its name is on a short list of headers that never carry a secret (default-deny: a header Primer sends itself, an
  operator-named MCP header, has no shape to recognise);
* ``url.query`` / ``http.query`` hold a bare query string, which has no ``?`` or ``&`` before its first parameter, so they are masked as a query.

Not covered (the masker recognises SHAPES): a query name outside the rule; a capability token that is simply part of a URL path other than ``/bot<id>:<secret>/``
and ``/v1/webhooks/<token>`` (an MCP server URL that embeds its key, a Slack or Discord webhook URL; ticket 01a1227f-ba04); header-shaped free text other than
Bearer and Basic (``x-api-key: <key>``, ``Authorization: Token <key>``, ``Bot <token>``); a secret of no shape in an attribute a tool sets itself.

A span with nothing to mask is exported as the very object the SDK made. A span with something to mask is exported as a thin view of it: the masked name,
attributes, events, links and status, and everything else (ids, timing, kind, resource, scope, trace state, every dropped count) read from the original.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status

from primer.common.log import redact_credentials

logger = logging.getLogger(__name__)

_REDACTED = "[REDACTED]"

# Attributes that hold a bare query string (``api_key=...&page=2``).
_QUERY_STRING_KEYS = frozenset({"url.query", "http.query"})

# Query parameter NAMES that redact_credentials does not list: OAuth codes and tokens, signatures, presigned-URL parts (SigV4 ``X-Amz-*``, GCS ``X-Goog-*``), vendor
# tokens. Matched whole and case-insensitively, after a ``?``, ``&`` or ``#`` (the first parameter of a URL fragment, ``#access_token=...``, is where an implicit-flow
# OAuth token arrives); the value runs to the next ``&``, ``#`` or space, and through a JSON ``\uXXXX`` escape as the shared rule does. A name that merely ENDS in one
# of these (``zipcode``, ``country_code``, ``msig``) is not matched: the boundary is the character before the name.
_SPAN_QUERY_SECRETS = re.compile(
    r"(?i)([?&#](?:signature|sig|code|auth[-_]?token|access[-_]?token|api_?token|access_?key|oauth_token|session_?token|id_token(?:_hint)?|private_token|jwt|passwd|hm"
    r"|hub\.verify_token|subscription[-_]key|x-amz-[a-z0-9-]+|x-goog-[a-z0-9-]+)=)"
    r"(?:[^&#\s'\"<>\\]|\\(?=u[0-9A-Fa-f]{4}))+"
)

# A Telegram bot token whose ``:`` is percent-encoded (``/bot123%3AAAH...``): redact_credentials only knows the plain ``:``.
_BOT_TOKEN_ENCODED = re.compile(r"(?i)(/bot)\d+%3A[A-Za-z0-9_-]+")

_HEADER_PREFIXES = ("http.request.header.", "http.response.header.")

# Headers that never carry a secret, by normalised name (lower case, ``-`` as ``_``). Every other captured header is masked.
_SAFE_HEADERS = frozenset({
    "accept", "accept_encoding", "accept_language", "cache_control", "connection", "content_encoding", "content_length", "content_type", "host",
    "traceparent", "tracestate", "user_agent", "x_request_id",
})


def _text(value: str) -> str:
    """``value`` with credentials masked (the same string object when there is nothing to mask); a value the masker cannot check is not exported."""
    try:
        masked = redact_credentials(value)
        masked = _BOT_TOKEN_ENCODED.sub(r"\1" + _REDACTED, masked)
        masked = _SPAN_QUERY_SECRETS.sub(r"\1" + _REDACTED, masked)
    except Exception:  # noqa: BLE001 - a span must never be lost, or leak, because the masker failed
        logger.exception("span_redaction: masking failed; the value is replaced")
        return _REDACTED
    return value if masked == value else masked


def _is_unsafe_header(key: str) -> bool:
    for prefix in _HEADER_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix):].lower().replace("-", "_") not in _SAFE_HEADERS
    return False


def _bare_query(value: str) -> str:
    """A bare query string masked as a query: a ``?`` in front gives its first parameter the anchor the rules need."""
    masked = _text("?" + value)
    return value if masked == "?" + value else masked.removeprefix("?")


def _value(key: str, value: Any, *, unsafe_header: bool) -> Any:
    """One attribute value, masked; the SAME object when nothing changed.

    Strings are masked (a captured header that is not on the safe list, whole), ``bytes`` as UTF-8 text and re-encoded only when something was masked, sequences
    and mappings element by element, everything else (numbers, booleans) is returned as it was.
    """
    if isinstance(value, str):
        if unsafe_header:
            return _REDACTED
        return _bare_query(value) if key in _QUERY_STRING_KEYS else _text(value)
    if isinstance(value, (bytes, bytearray)):
        if unsafe_header:
            return b"[REDACTED]"
        text = bytes(value).decode("utf-8", "replace")
        masked = _text(text)
        return value if masked == text else masked.encode("utf-8")
    if isinstance(value, Mapping):
        items = {k: _value(key, v, unsafe_header=unsafe_header) for k, v in value.items()}
        return value if all(items[k] is value[k] for k in items) else items
    if isinstance(value, Sequence):
        elements = tuple(_value(key, v, unsafe_header=unsafe_header) for v in value)
        return value if all(a is b for a, b in zip(elements, value, strict=True)) else elements
    return value


def _attributes(attributes: Mapping[str, Any] | None) -> tuple[Mapping[str, Any] | None, bool]:
    """``(attributes, changed)``: the masked mapping, or the original object when no value changed."""
    if not attributes:
        return attributes, False
    masked = {k: _value(k, v, unsafe_header=_is_unsafe_header(k)) for k, v in attributes.items()}
    changed = any(masked[k] is not attributes[k] for k in masked)
    return (masked, True) if changed else (attributes, False)


class _RedactedEvent(Event):
    """An event with masked attributes; the dropped-attribute count is the original's."""

    def __init__(self, source: Event, attributes: Mapping[str, Any] | None) -> None:
        super().__init__(name=_text(source.name), attributes=attributes, timestamp=source.timestamp)
        self._source = source

    @property
    def dropped_attributes(self) -> int:
        return self._source.dropped_attributes


class _RedactedLink(Link):
    """A link with masked attributes; the dropped-attribute count is the original's."""

    def __init__(self, source: Link, attributes: Mapping[str, Any] | None) -> None:
        super().__init__(source.context, attributes)
        self._source = source

    @property
    def dropped_attributes(self) -> int:
        return self._source.dropped_attributes


class _RedactedSpan(ReadableSpan):
    """A view of a finished span with a masked name, attributes, events, links and status; everything else is the original's.

    ``ReadableSpan.__init__`` is deliberately not called: its signature (it still takes a deprecated argument) is the SDK's to change. The five masked fields are
    set here; every other private field the SDK's properties and ``to_json`` read (ids, timing, kind, resource, scope) falls through to the source span.
    """

    def __init__(
        self, span: ReadableSpan, *, name: str, attributes: Mapping[str, Any] | None, events: Sequence[Event], links: Sequence[Link], status: Status,
    ) -> None:
        self._source = span
        self._name = name
        self._attributes = attributes
        self._events = events
        self._links = links
        self._status = status

    def __getattr__(self, item: str) -> Any:
        if item == "_source" or (item.startswith("__") and item.endswith("__")):
            raise AttributeError(item)
        return getattr(self._source, item)

    @property
    def dropped_attributes(self) -> int:
        return self._source.dropped_attributes

    @property
    def dropped_events(self) -> int:
        return self._source.dropped_events

    @property
    def dropped_links(self) -> int:
        return self._source.dropped_links


def redact_span(span: ReadableSpan) -> ReadableSpan:
    """``span`` itself when it carries nothing to mask, else a :class:`_RedactedSpan` of it."""
    name = _text(span.name)
    attributes, attributes_changed = _attributes(span.attributes)

    events: list[Event] = []
    events_changed = False
    for event in span.events:
        masked, changed = _attributes(event.attributes)
        renamed = _text(event.name)
        if changed or renamed != event.name:
            events.append(_RedactedEvent(event, masked))
            events_changed = True
        else:
            events.append(event)

    links: list[Link] = []
    links_changed = False
    for link in span.links:
        masked, changed = _attributes(link.attributes)
        if changed:
            links.append(_RedactedLink(link, masked))
            links_changed = True
        else:
            links.append(link)

    status = span.status
    description = _text(status.description) if status.description else status.description
    status_changed = description != status.description

    if not (name != span.name or attributes_changed or events_changed or links_changed or status_changed):
        return span
    return _RedactedSpan(
        span,
        name=name,
        attributes=attributes,
        events=events,
        links=links,
        status=Status(status.status_code, description) if status_changed else status,
    )


class RedactingSpanExporter(SpanExporter):
    """Wraps ``inner``: the spans it is handed are exported with every credential masked; the result, flush and shutdown are the inner exporter's."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        # One span that cannot be masked must not cost the up-to-512 spans batched with it: it is dropped (never exported unmasked), logged, and the rest go on.
        masked: list[ReadableSpan] = []
        for span in spans:
            try:
                masked.append(redact_span(span))
            except Exception:  # noqa: BLE001 - fail closed for this span only
                context = getattr(span, "context", None)
                logger.exception(
                    "span_redaction: a span could not be masked and was dropped (span_id=%s)",
                    format(context.span_id, "016x") if context is not None else "?",
                )
        return self._inner.export(masked)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._inner.shutdown()


__all__ = ["RedactingSpanExporter", "redact_span"]
