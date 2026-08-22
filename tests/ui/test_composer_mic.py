"""Push-to-talk mic in the composer (S4 section 5).

Gating rule: the mic renders only when an STT provider is configured,
read off the /capabilities speech block, not off an extra. Transcribed
text lands in the composer for review; there is no auto-send in v1.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_the_composer_accepts_the_transcription_sink() -> None:
    src = _read("components/shared/composer.jsx")
    assert "onTranscribed" in src


def test_the_mic_button_is_gated_on_the_capability_flag() -> None:
    src = _read("components/shared/composer.jsx")
    assert "useCapabilities" in src
    assert "stt_configured" in src
    assert "micEnabled &&" in src or "{micEnabled" in src
    assert 'data-testid="chat-mic-btn"' in src


def test_the_capture_path_downmixes_and_resamples_before_upload() -> None:
    src = _read("components/shared/composer.jsx")
    assert "PRIMER_toMono16kWav" in src


def test_long_captures_are_segmented_on_silence() -> None:
    src = _read("components/shared/composer.jsx")
    assert "PRIMER_findSilenceSplit" in src


def test_the_upload_uses_the_server_proxy_not_a_provider_url() -> None:
    src = _read("components/shared/composer.jsx")
    assert "/audio/transcriptions" in src
    assert "asr" not in src.lower().replace("asr-tts", ""), (
        "the browser must never see a provider URL; it talks to the proxy"
    )


def test_transcribed_text_lands_in_the_draft_and_is_not_auto_sent() -> None:
    src = _read("components/shared/composer.jsx")
    assert "onTranscribed" in src
    # onSend must not be reachable from the transcription handler.
    handler_start = src.index("onTranscribed")
    window_src = src[handler_start: handler_start + 600]
    assert "onSend(" not in window_src, "v1 has no auto-send after transcription"


def test_the_composer_is_the_only_host_the_mic_needs() -> None:
    """S1 P6 relocated this file and S1 P7 deleted its old chat host
    (crosscheck findings F15/F28/F43), and S8 deletes the studio panels
    that host it today. The mic code must therefore depend on no host and
    no shell: it reads a capability and posts to the proxy, nothing else.

    Scoped to the mic block on purpose: the file carries pre-existing prose
    comments from its chat-refactor history, and rewriting those is S1's
    business, not this task's.
    """
    src = _read("components/shared/composer.jsx")
    mic_at = src.index("CT_captureMicSegment")
    block = src[mic_at: mic_at + 3000]
    assert "window.location" not in block
    assert "studio" not in block
    assert "conversation" not in block


def test_the_mic_gate_is_not_frozen_at_page_load() -> None:
    """Regression: registering a speech provider needed a page reload.

    The mic hangs off capabilities.speech.stt_configured, which is "does
    a provider row exist" and changes while the console is open. The
    capabilities resource was fetched once and never again, so the answer
    was a snapshot from whenever the page happened to load: register a
    provider, go back to a session, and the mic was still missing.

    The extras block genuinely IS per-process, which is why it was
    written that way; the speech block joining it is what made the
    freeze wrong.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "ui" / "foundation"
           / "capabilities.js").read_text(encoding="utf-8")
    block = src[src.index("function useCapabilities()"):]
    block = block[:block.index("function extraInstalled")]
    assert "pollMs: 0" not in block, (
        "a gate on a mutable fact cannot be answered once and cached "
        "for the life of the page"
    )
    assert "pollMs: 10000" in block
