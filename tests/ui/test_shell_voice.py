"""Voice (spec section 8), which is mostly a list of things NOT to do.

Dictation always lands as editable text; release never auto-sends. TTS
is per-turn opt-in plus a per-session toggle that auto-plays FINAL
ANSWERS ONLY, never tool narration and never a background session. Every
affordance is capability-gated, so an unconfigured server shows nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-voice.jsx"
SESSION = UI / "components" / "shell" / "sh-session-doc.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_registered_in_the_bundle() -> None:
    assert 'src="components/shell/sh-voice.jsx"' in (
        UI / "index.html"
    ).read_text(encoding="utf-8")


def test_every_affordance_is_capability_gated() -> None:
    src = _src()
    assert "tts_configured" in src
    assert "stt_configured" in SESSION.read_text(encoding="utf-8")


def test_dictation_never_auto_sends() -> None:
    """Prohibited: open-mic VAD auto-commit, catastrophic where an
    utterance can be an approval."""
    session = SESSION.read_text(encoding="utf-8")
    assert "onTranscribed" in session
    assert "onSend" not in session.split("onTranscribed")[1].split("}")[0]
    src = _src()
    for banned in ("VAD", "autoSend", "auto_send", "silenceDetect"):
        assert banned not in src, banned


def test_auto_play_is_final_answers_only_and_foreground_only() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    body = _src()
    start = body.index("function SH_shouldAutoPlay")
    end = body.index("function SH_SpeakerButton")
    ctx.eval(body[start:end])

    def call(kind: str, enabled: bool, fg: bool) -> bool:
        return json.loads(ctx.eval(
            "JSON.stringify(SH_shouldAutoPlay({row: {kind: %s, final: true}, "
            "enabled: %s, isForeground: %s}))"
            % (json.dumps(kind), json.dumps(enabled), json.dumps(fg))
        ))

    assert call("assistant_message", True, True) is True
    assert call("tool_call", True, True) is False, "never tool narration"
    assert call("assistant_message", True, False) is False, (
        "never a background session"
    )
    assert call("assistant_message", False, True) is False


def test_a_persistent_stop_control_exists() -> None:
    src = _src()
    assert 'data-testid="shell-voice-stop"' in src
    assert ".pause()" in src


def test_per_turn_opt_in_and_a_per_session_toggle_both_exist() -> None:
    src = _src()
    assert 'data-testid={"shell-speak:"' in src
    assert 'data-testid="shell-voice-toggle"' in src
    assert "CT_speakTurn" in src


def test_gated_verbs_still_need_an_explicit_confirmation() -> None:
    """Section 8: approve always requires explicit confirmation
    regardless of mode. Voice must not be able to run a decision."""
    src = _src()
    assert "SH_api.approve" not in src
    assert "decision" not in src
