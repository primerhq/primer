"""A workspace__ tool is injected per session and never appears in the tool catalogue, so the live
builder must not warn "isn't in the tool catalogue" for it; the retired legacy editor exempted
them, and this file pins that on the live validator. It is a separate file (not an addition to
tests/ui/test_graph_builder_parity.py) because another PR edits that one.
"""

from __future__ import annotations

from tests.ui.test_graph_builder_parity import _BEGIN, _END, _edge, _validate


def _draft(tool_id: str) -> dict:
    return {
        "nodes": [_BEGIN, {"kind": "tool_call", "id": "t", "tool_id": tool_id, "arguments": {}}, _END],
        "edges": [_edge("s", "t"), _edge("t", "e")],
    }


def test_a_workspace_tool_is_not_warned_as_unknown() -> None:
    # workspace__ ids are per-session injections, absent from the catalogue by design.
    result = _validate(_draft("workspace__files_read"), {"knownToolIds": ["toolset__real"]})
    assert "tool_unknown" not in [row["code"] for row in result["warnings"]]


def test_a_tool_that_is_not_in_the_catalogue_is_still_warned() -> None:
    result = _validate(_draft("toolset__nope"), {"knownToolIds": ["toolset__real"]})
    assert "tool_unknown" in [row["code"] for row in result["warnings"]]
