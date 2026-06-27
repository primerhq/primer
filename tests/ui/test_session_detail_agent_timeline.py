"""Agent sessions gain a TurnRow-fed turn timeline + a clear status line
(running / waiting-on-<gate> / ended / failed). TurnRow was unused; it is
now activated. Graph sessions are unaffected."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DETAIL = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")


def test_turn_timeline_component_present() -> None:
    assert "function SD_AgentTurnTimeline" in DETAIL
    assert "window.SD_AgentTurnTimeline" in DETAIL


def test_timeline_activates_turnrow() -> None:
    # TurnRow was defined-but-unused; the timeline now renders it.
    assert "<TurnRow" in DETAIL


def test_timeline_uses_session_turn_log() -> None:
    assert "/turn_log" in DETAIL


def test_status_line_present() -> None:
    assert "function SD_AgentStatusLine" in DETAIL
    for token in ("running", "waiting", "ended", "failed"):
        assert token in DETAIL


def test_timeline_gated_for_agent_only() -> None:
    # Rendered behind a !isGraph gate so graph sessions keep the run view.
    assert "isGraph" in DETAIL
    assert "SD_AgentTurnTimeline" in DETAIL


def test_bundle_transpiles_with_timeline() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "SD_AgentTurnTimeline" in body.decode("utf-8")
