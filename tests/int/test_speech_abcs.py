"""The ASR / TTS ABCs follow the int/llm.py template (S4 P1 Task 4)."""

from __future__ import annotations

import inspect

import pytest

from primer.int import ASR, TTS
from primer.model.speech import SpeechError, Transcription


def test_the_abcs_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        ASR()
    with pytest.raises(TypeError):
        TTS()


def test_transcribe_is_keyword_only_and_carries_the_multipart_triple() -> None:
    sig = inspect.signature(ASR.transcribe)
    params = list(sig.parameters)
    assert params[0] == "self"
    for name in ("model", "audio", "filename", "mimetype", "language"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["language"].default is None


def test_tts_stream_is_keyword_only_and_defaults_to_mp3() -> None:
    sig = inspect.signature(TTS.stream)
    for name in ("model", "text", "voice", "response_format"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["response_format"].default == "mp3"


@pytest.mark.asyncio
async def test_aclose_defaults_to_a_no_op_on_both() -> None:
    class _ASR(ASR):
        async def transcribe(self, *, model, audio, filename, mimetype, language=None):
            return Transcription(text="ok")

    class _TTS(TTS):
        async def stream(self, *, model, text, voice, response_format="mp3"):
            yield b"x"

    assert await _ASR().aclose() is None
    assert await _TTS().aclose() is None


@pytest.mark.asyncio
async def test_a_concrete_asr_may_report_failure_as_a_value() -> None:
    class _Failing(ASR):
        async def transcribe(self, *, model, audio, filename, mimetype, language=None):
            return SpeechError(code="warming_up", message="loading", retriable=True)

    out = await _Failing().transcribe(
        model="whisper-1", audio=b"", filename="a.wav", mimetype="audio/wav",
    )
    assert isinstance(out, SpeechError)
    assert out.retriable is True


@pytest.mark.asyncio
async def test_a_concrete_tts_may_yield_a_terminal_error_value() -> None:
    class _Failing(TTS):
        async def stream(self, *, model, text, voice, response_format="mp3"):
            yield b"partial"
            yield SpeechError(code="provider_error", message="dropped")

    got = [item async for item in _Failing().stream(model="k", text="hi", voice="v")]
    assert got[0] == b"partial"
    assert isinstance(got[1], SpeechError)
