"""What ``GET /v1/sessions/{id}/messages`` items look like, for the tests that read them (the long-session e2e journey and the unit run that pins these shapes).

The route serves the raw log: two shapes are interleaved in it. A SessionMessageRecord row has ``seq``, ``kind`` and ``payload`` (``user_input``, ``assistant_token``,
``llm_call``, ``done``, ...). A plain ``Message`` line, the model's history, has ``role`` and ``parts`` and NO ``kind`` and NO ``seq``. The route drops a user Message line whose
text a ``user_input`` record already carries, so every instruction appears once, as its record; an assistant Message line is passed through untouched, one per assistant
message, with the whole text (the ``assistant_token`` records are the streamed pieces of it, however they were coalesced).
"""

from __future__ import annotations

from typing import Any


def record_seqs(items: list[dict[str, Any]]) -> list[int]:
    """The ``seq`` of every record row, in route order (Message lines carry none)."""
    return [r["seq"] for r in items if isinstance(r.get("seq"), int)]


def user_inputs(items: list[dict[str, Any]]) -> list[str]:
    """The text of every ``user_input`` record: one per instruction or steer, exactly once by construction."""
    return [r["payload"]["text"] for r in items if r.get("kind") == "user_input"]


def assistant_answers(items: list[dict[str, Any]]) -> list[str]:
    """The text of every assistant ``Message`` line: one per assistant message, so one per plain answer, exactly once by construction."""
    return ["".join(p.get("text", "") for p in r.get("parts", []) if p.get("type") == "text") for r in items if r.get("role") == "assistant"]
