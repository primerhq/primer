"""The row's ``max_retries`` is the only retry policy for ASR and TTS (Part 2 T50, W50).

Both clients were built with the SDK default of 2 retries: ASR ignored the row (``max_retries: 0`` still sent three
requests) and TTS multiplied it (its own loop of ``max_retries + 1`` around the SDK's three, so 2 meant nine).
"""

from __future__ import annotations

import pytest

import primer.speech.openai_asr as asr_mod
import primer.speech.openai_tts as tts_mod
from primer.model.speech import SpeechError
from tests.speech.test_speech_wire_characterization import _HTTP, _asr, _route, _speak, _transcribe, _tts


def _refused() -> Exception:
    return _HTTP.ConnectError("connection refused")


@pytest.mark.parametrize(("max_retries", "attempts"), [(0, 1), (2, 3)])
async def test_asr_makes_max_retries_plus_one_attempts(monkeypatch, max_retries, attempts) -> None:
    wire = _route(monkeypatch, asr_mod, [_refused()])
    assert isinstance(await _transcribe(_asr(max_retries=max_retries)), SpeechError)
    assert len(wire.requests) == attempts


@pytest.mark.parametrize(("max_retries", "attempts"), [(0, 1), (2, 3)])
async def test_tts_makes_max_retries_plus_one_attempts(monkeypatch, max_retries, attempts) -> None:
    wire = _route(monkeypatch, tts_mod, [_refused()])
    got = await _speak(_tts(max_retries=max_retries))
    assert len(got) == 1 and isinstance(got[0], SpeechError)
    assert len(wire.requests) == attempts


async def test_asr_max_retries_zero_sends_a_429_once(monkeypatch) -> None:
    wire = _route(monkeypatch, asr_mod, [(429, b'{"error": {"message": "slow down"}}', {"retry-after-ms": "1"})])
    assert isinstance(await _transcribe(_asr(max_retries=0)), SpeechError)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("make", [_asr, _tts])
async def test_the_client_is_built_with_no_sdk_retries(make) -> None:
    adapter = make()
    try:
        assert adapter._get_client().max_retries == 0
    finally:
        await adapter.aclose()
