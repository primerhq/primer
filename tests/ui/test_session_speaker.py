"""Per-session speaker toggle streams completed turns as chunked mp3.

The toggle lives in the shared session-controls module (S1 P6 Task 33),
not in a chat page: S1 P7 deleted conversation.jsx and S8 deletes the
studio panels, so anything that must survive both lands here.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_the_speaker_toggle_is_gated_on_tts_configured() -> None:
    src = _read("components/shared/session-controls.jsx")
    assert "tts_configured" in src
    assert 'data-testid="chat-speaker-toggle"' in src


def test_playback_streams_through_an_audio_element() -> None:
    src = _read("components/shared/session-controls.jsx")
    assert "SC_speakTurn" in src
    assert "new Audio(" in src or "document.createElement(\"audio\")" in src


def test_playback_consumes_the_response_incrementally() -> None:
    """S4 section 5: the reply is READ ALOUD as it synthesises. The
    server pump yields chunk-by-chunk (Task 13), so a client that awaits
    response.blob() throws that away and reintroduces the whole latency
    the yield-through pattern exists to remove."""
    src = _read("components/shared/session-controls.jsx")
    speak_at = src.index("function SC_speakTurn")
    window_src = src[speak_at: speak_at + 2500]
    assert "MediaSource" in window_src
    assert "getReader()" in window_src


def test_playback_targets_the_server_proxy() -> None:
    src = _read("components/shared/session-controls.jsx")
    assert "/audio/speech" in src


def test_the_agent_id_is_forwarded_so_the_override_can_apply() -> None:
    src = _read("components/shared/session-controls.jsx")
    assert "agent_id" in src


def test_the_toggle_is_per_session_state_not_a_global() -> None:
    src = _read("components/shared/session-controls.jsx")
    assert "React.useState(false)" in src
    assert "speakerOn" in src


def test_the_speaker_block_stays_props_only() -> None:
    """S1 pinned decision 17 and amendment M9: the shared session modules
    take props, so S8's fresh shell re-hosts them unchanged. Scoped to the
    block this task adds; the module-wide version of this rule is S1 Task
    33's own guard (tests/ui/test_session_controls.py), which runs beside
    this module."""
    src = _read("components/shared/session-controls.jsx")
    speak_at = src.index("SessionSpeakerToggle")
    block = src[speak_at: speak_at + 2000]
    assert "window.location" not in block
    assert "ROUTES" not in block
    assert "studio" not in block


def test_the_toggle_is_exported_as_a_window_global() -> None:
    """The global is the contract every host binds to. Deliberately NOT a
    read of studio-center.jsx: S8 P5 deletes that file, and a static test
    that opens it becomes a FileNotFoundError at the flag day (crosscheck
    findings F22/F31). The transitional mount is covered by the Task 27
    journey instead."""
    src = _read("components/shared/session-controls.jsx")
    assert "window.SessionSpeakerToggle" in src


def test_audio_is_ephemeral_and_never_written_to_history() -> None:
    """S4 section 5: text is the record; history is unchanged."""
    src = _read("components/shared/session-controls.jsx")
    speak_at = src.index("SC_speakTurn")
    window_src = src[speak_at: speak_at + 800]
    for persisting in ("appendMessage", "setMessages", "history.push"):
        assert persisting not in window_src, (
            "synthesised audio must not enter the transcript"
        )
