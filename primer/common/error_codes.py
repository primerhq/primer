"""What an error ``code`` may look like (security ticket 01a11fbc-ffea).

A ``code`` is a short machine identifier. Ours always are (``rate_limit``, ``server_error``, ``llm_stream_error``); a provider's real ones are too
(``context_length_exceeded``, ``rate_limited``, a JSON-RPC number as text). A provider can also send anything else: an OpenResponses ``error`` event's
``code`` and the OpenAI SDK exception's ``code`` (the body's ``error.code``) were passed through whole, unbounded and with any character, into the ERROR
record, ``ended_detail``, ``last_turn_error``, the ``session.turn_failed`` payload and, for a graph, the ``X-Primer-Graph-Ended-Detail`` git trailer
(``f"{key}: {value}"``, so a newline in a code injected trailer lines).

:func:`safe_code` is the one rule: a code survives only when it is ``[A-Za-z0-9_.:-]{1,64}``, else it becomes the fixed sentinel :data:`UNSAFE_CODE`.
NOT ``None``: callers treat "no code" as "classify the failure by its class" (a 5xx is then ``server_error``, which RESTS an interactive session and is
transient for strict aggregated failover), while a code that IS set and that nobody classified ENDS the session and is only config-eligible for
failover. A hostile code must not flip the rule it is judged by, so it stays a set, unclassified code. An empty code is no code.
"""

from __future__ import annotations

import re


_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

UNSAFE_CODE = "provider_error"
"""What a code that is not an identifier becomes. Not in any set of resting or transient codes, so it keeps today's "set but unclassified" rule."""


def safe_code(value: object) -> str | None:
    """``value`` when it is an identifier-shaped string (at most 64 characters of ``A-Za-z0-9_.:-``); ``None`` for no code (``None`` or ``""``); else :data:`UNSAFE_CODE`.

    An int (a proxy's ``"code": 429``) is read as its text. Any other type is not an identifier and becomes the sentinel.
    """
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
        return value
    return UNSAFE_CODE


__all__ = ["UNSAFE_CODE", "safe_code"]
