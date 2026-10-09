"""What an error ``code`` may look like (security ticket 01a11fbc-ffea).

A ``code`` is a short machine identifier. Ours always are (``rate_limit``, ``server_error``, ``llm_stream_error``); a provider's real ones are too
(``context_length_exceeded``, ``rate_limited``, a JSON-RPC number as text). A provider can also send anything else: an OpenResponses ``error`` event's
``code`` and the OpenAI SDK exception's ``code`` (the body's ``error.code``) were passed through whole, unbounded and with any character, into the ERROR
record, ``ended_detail``, ``last_turn_error``, the ``session.turn_failed`` payload and, for a graph, the ``X-Primer-Graph-Ended-Detail`` git trailer
(``f"{key}: {value}"``, so a newline in a code injected trailer lines).

:func:`safe_code` is the one rule: a code survives only when it is ``[A-Za-z0-9_.:-]{1,64}``, else it is ``None`` and every caller already handles "no
code" (it classifies the failure by its class instead).
"""

from __future__ import annotations

import re


_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def safe_code(value: object) -> str | None:
    """``value`` when it is an identifier-shaped string (at most 64 characters of ``A-Za-z0-9_.:-``), else ``None``."""
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


__all__ = ["safe_code"]
