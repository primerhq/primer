"""The shared session-state decoder turns a session row into a render-ready
descriptor (outcome / waiting-on / running), consumed by both the list and
the detail page. Pure, frontend-only — derived from fields already on the row.
Project convention: source-grep the decode rules + a bundle-transpile check."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = (UI / "components" / "session-state.jsx").read_text(encoding="utf-8")
INDEX = (UI / "index.html").read_text(encoding="utf-8")


def _order() -> list[str]:
    out: list[str] = []
    for line in INDEX.splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            s = line.index('src="') + 5
            out.append(line[s:line.index('"', s)])
    return out


def test_exports_decoder_and_countdown() -> None:
    assert "function describeSessionState" in SRC
    assert "window.describeSessionState" in SRC
    assert "SessionCountdown" in SRC
    assert "window.SessionCountdown" in SRC


def test_decodes_ended_detail_codes() -> None:
    # Known graph/agent failure codes map to human text; unknown -> verbatim.
    for code in (
        "routing_failed", "max_iterations_exceeded", "node_failed",
        "fanin_upstream_failed", "tool_execution_failed",
    ):
        assert code in SRC


def test_covers_park_prefixes_and_attention() -> None:
    for prefix in ("ask_user:", "tool_approval:", "timer:", "watch:", "mcp_task:"):
        assert prefix in SRC
    assert "needsAttention" in SRC


def test_emits_groups_and_tones() -> None:
    for token in ("running", "waiting", "ended", "failed", "cancelled", "idle"):
        assert token in SRC
    assert "countdownTo" in SRC
    assert "waitingOn" in SRC


def test_a_tool_turn_cap_end_is_not_read_as_completed() -> None:
    """An autonomous session ended by max_tool_turns carries ended_reason "tool_turn_cap" and status "ended":
    the decoder must name it before the status === "ended" fall-through that reads as Completed (green)."""
    cap = SRC.index('reason === "tool_turn_cap"')
    completed = SRC.index('label: "Completed"')
    assert cap < completed
    branch = SRC[cap:completed]
    assert 'group: "failed"' in branch and "tool-turn cap" in branch


def test_registered_before_list_and_detail() -> None:
    order = _order()
    assert "components/session-state.jsx" in order
    i = order.index("components/session-state.jsx")
    assert i < order.index("components/sessions-list.jsx")
    assert i < order.index("components/session-detail.jsx")


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "/* === components/session-state.jsx === */" in body.decode("utf-8")
