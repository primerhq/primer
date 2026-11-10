"""The access-matrix MCP door must classify the M2 nested call_tool envelope.

The e2e door (tests/e2e/test_access_matrix_journey.py) drives every target
through the advertised ``call_tool`` meta-tool and maps the fixed M2 wire
onto its three verdicts (ok / missing / denied): a not-found inner result
and a not-exposed tool both answer like a missing row (the non-leak: the
wire never echoes existence), and a refusal answers denied. A regression in
this mapping would otherwise surface only in the live e2e lane (minutes),
so the mapping is pinned here against canned envelopes (seconds).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from tests.e2e.test_access_matrix_journey import DENIED, MISSING, OK, McpDoor

_TARGET = "system__get_agent"


def _door_result(payload: dict) -> SimpleNamespace:
    """One CallToolResult-shaped value: the M2 meta-tool text content (isError tracks the envelope)."""
    return SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(payload))],
        is_error=payload.get("ok") is not True,
    )


def _tool_failed(inner: dict | str) -> dict:
    """The M2 refusal for a failing inner tool: the inner output nested verbatim in the message."""
    return {
        "ok": False,
        "error": {"code": "tool_failed", "message": json.dumps(inner) if isinstance(inner, dict) else inner},
    }


class _CannedSession:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def call_tool(self, name: str, arguments: dict):
        assert name == "call_tool" and arguments == {"scoped_id": _TARGET, "arguments": {}}
        return _door_result(self._payload)


async def test_the_mcp_door_classifies_the_m2_call_tool_envelope() -> None:
    async def classify(payload: dict) -> str:
        return (await McpDoor(_CannedSession(payload))._call(_TARGET, {}))[0]

    # success: the nested ok envelope carries the inner output
    assert await classify({"ok": True, "output": "row"}) == OK
    # the inner {"type", "message"} error result, nested in tool_failed
    assert await classify(_tool_failed({"type": "not-found", "message": "Agent does not exist"})) == MISSING
    assert await classify(_tool_failed({"type": "forbidden", "message": "Agent is not yours to edit"})) == DENIED
    # the trigger get tool's own not-found word (access spec 9: no access answers like missing)
    assert await classify(_tool_failed({"type": "trigger_not_found", "message": "Trigger 'x' does not exist"})) == MISSING
    # an RFC7807-shaped inner result carries a numeric status instead of a type
    assert await classify(_tool_failed({"status": 404, "title": "Not Found", "detail": "no such row"})) == MISSING
    assert await classify(_tool_failed({"status": 403, "title": "Forbidden", "detail": "level below owner"})) == DENIED
    # plain-text inner output falls back to the door's substring words
    assert await classify(_tool_failed("workspace session not found")) == MISSING
    assert await classify(_tool_failed("this operation is forbidden for your role")) == DENIED
    # the fixed meta-level refusals
    assert await classify({"ok": False, "error": {"code": "access_denied", "message": "Access to this tool is denied."}}) == DENIED
    assert await classify({"ok": False, "error": {"code": "tool_unavailable", "message": "Tool is unavailable."}}) == MISSING
    # an unknown inner type fails loudly, never masked as missing or denied
    assert await classify(_tool_failed({"type": "boom", "message": "x"})) == "tool-error-boom"
