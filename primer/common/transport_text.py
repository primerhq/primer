"""The text of a transport error a keyed adapter raises, with the adapter's own credentials masked (ticket 01a12010).

httpx's errors carry what went wrong with the request, and a pasted API key that ends in a newline makes h11 refuse the header with
``LocalProtocolError: Illegal header value b'<the key>\\n'``. An adapter that puts ``str(exc)`` in the exception it raises hands the key to every
caller of that exception: the tool result the agent reads, the session transcript, the model vendor, the logs of the service and the tool.

The mask is the one the LLM adapters use for the text a provider sends back (``primer.llm._failure.scrubbed_event_text``): the configured ``api_key``
in the forms a message can show it (itself, its ``repr``-escaped form, its whitespace-normalised form), URL userinfo, ``Bearer`` and ``Basic`` tokens.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from primer.llm._failure import scrubbed_event_text

__all__ = ["transport_failure"]


def transport_failure(label: str, exc: BaseException, config: Any) -> str:
    """``"<label> transport: <ExceptionType>: <text>"`` with the credentials ``config`` holds (its ``api_key``) masked.

    The reason (the exception's type and its words) is kept; only the secret goes. ``config`` is the adapter's own config object: the scrub reads
    ``config.api_key`` (a ``SecretStr``, or ``None`` for a keyless adapter) and ``config.url`` if it has one.
    """
    return scrubbed_event_text(f"{label} transport: {type(exc).__name__}: {exc}", SimpleNamespace(config=config))
