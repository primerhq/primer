"""What a keyed adapter does around the HTTP call that carries its API key (ticket 01a12010, #686): refuse a key that cannot be sent, and keep the key out of the text of
what comes back.

A pasted API key often ends in a newline. httpx hands it to h11 as a header value and h11 refuses it with ``LocalProtocolError: Illegal header value b'<the key>\\n'``.
An adapter that puts ``str(exc)`` in the exception it raises hands the key to every caller of that exception (the tool result the agent reads, the session transcript, the
model vendor, the logs of the service and the tool), and the OpenTelemetry httpx client span records the raw exception (status description, ``exception.message``, stack
trace) when OTLP export is on. Two layers:

* :func:`require_sendable_key` runs BEFORE the request: a key h11 would reject (surrounding whitespace, a control character, a non-ASCII character) is refused with the
  provider error, with no key in the text. No request is made, so there is no span, no ``httpcore`` DEBUG line and no ``UnicodeEncodeError``.
* :func:`transport_failure` and :func:`unexpected_status` mask the key an adapter holds in the text of what it raises (a transport that echoes the request, a vendor's body
  that quotes the key), through the rule the LLM adapters use for a provider's text (``primer.llm._failure.scrubbed_event_text``): the configured ``api_key`` in the forms
  a message can show it (itself, its ``repr``-escaped form, its whitespace-normalised form), URL userinfo, ``Bearer`` and ``Basic`` tokens.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr

from primer.llm._failure import scrubbed_event_text

__all__ = ["UNSENDABLE_KEY", "require_sendable_key", "transport_failure", "unexpected_status"]

#: What a refused key is told, after the adapter's name. It names no key.
UNSENDABLE_KEY = "api_key has surrounding whitespace or a control/non-ASCII character; re-enter it"

#: Longest part of a vendor's non-2xx body an adapter's message shows (what it showed before the scrub moved in front of the cut).
_BODY_CHARS = 200


def require_sendable_key(label: str, key: SecretStr | None, error: type[Exception]) -> None:
    """Raise ``error("<label> api_key has surrounding whitespace ...")`` when ``key`` is one h11 would reject as a header value; a missing key (an optional one) passes.

    Refused: any surrounding whitespace, any control character (a pasted ``\\n``, ``\\r\\n``, ``\\t``, NUL), anything not ASCII. An inner space is a legal header value and passes.
    The text carries no part of the key.
    """
    if key is None:
        return
    value = key.get_secret_value()
    if value != value.strip() or not value.isascii() or not value.isprintable():
        raise error(f"{label} {UNSENDABLE_KEY}")


def transport_failure(label: str, exc: BaseException, config: Any) -> str:
    """``"<label> transport: <ExceptionType>: <text>"`` with the credentials ``config`` holds (its ``api_key``) masked.

    The reason (the exception's type and its words) is kept; only the secret goes. ``config`` is the adapter's own config object: the scrub reads
    ``config.api_key`` (a ``SecretStr``, or ``None`` for a keyless adapter) and ``config.url`` if it has one. The caller raises ``from None``, so the raw exception is not
    chained: a traceback printed anywhere shows this text, not the library's.
    """
    return scrubbed_event_text(f"{label} transport: {type(exc).__name__}: {exc}", SimpleNamespace(config=config))


def unexpected_status(label: str, status_code: int, body: str, config: Any, limit: int = _BODY_CHARS) -> str:
    """``"<label> unexpected status <code>: <body>"`` with the body scrubbed FIRST and cut to ``limit`` characters AFTER.

    A cut made before the scrub can leave the head of a key that straddles the limit; the scrub sees the whole body (to its own scan limit) and the cut then falls on text that
    holds no secret. The message stays as short as it always was.
    """
    shown = scrubbed_event_text(body, SimpleNamespace(config=config))[:limit]
    return f"{label} unexpected status {status_code}: {shown}"
