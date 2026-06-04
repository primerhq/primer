"""Shared tracing helpers for LLM adapters.

The OTEL span attribute ``llm.request.messages`` carries a JSON-encoded
copy of the request messages when the operator opts in via
``trace_llm_io=True``. The serialiser here flattens the universal
``Message`` shape into a JSON-safe structure (text content is kept;
non-text parts are reduced to their type name to avoid leaking large
binary payloads into spans).

Every LLM adapter needs the same serialisation when the trace flag is
set. Previously this helper was duplicated verbatim across five
adapter modules; this module is the single source of truth.
"""

from __future__ import annotations

from typing import Any

from primer.model.chat import Message, TextPart


def _serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize a list of universal Messages to a JSON-safe structure."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        parts: list[dict[str, Any]] = []
        for part in msg.parts:
            if isinstance(part, TextPart):
                parts.append({"type": "text", "text": part.text})
            else:
                parts.append({"type": type(part).__name__})
        out.append({"role": msg.role, "parts": parts})
    return out


__all__ = ["_serialize_messages"]
