"""CV-1: a speech provider's failure text is scrubbed of the row's credentials and capped, like the LLM path's (primer.llm._failure)."""
import httpx
import pytest
from openai import AsyncOpenAI

from primer.model.provider import SpeechToTextProvider, TextToSpeechProvider
from primer.model.speech import SpeechError
from primer.speech.openai_asr import OpenAIASR
from primer.speech.openai_tts import OpenAITTS

pytestmark = pytest.mark.asyncio
KEY = "sk-live-SECRET-0123456789"


def _stt():
    return SpeechToTextProvider(id="stt1", provider="openai", default_model="whisper-1", config={"url": "http://asr.internal:9000/v1", "api_key": KEY},
                                limits={"max_concurrency": 1, "max_retries": 0})


def _tts():
    return TextToSpeechProvider(id="tts1", provider="openai", default_model="tts-1", default_voice="alloy",
                                config={"url": "http://tts.internal:9000/v1", "api_key": KEY}, limits={"max_concurrency": 1, "max_retries": 0})


def _client(handler):
    return AsyncOpenAI(base_url="http://x.internal/v1", api_key=KEY, max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _echo(request):
    return httpx.Response(400, json={"error": {"message": f"Bad Authorization header 'Bearer {KEY}'", "type": "invalid_request_error"}})


def _page(request):
    return httpx.Response(404, text="<html>" + "x" * 500_000 + "</html>", headers={"content-type": "text/html"})


async def _asr(handler):
    a = OpenAIASR(_stt())
    a._client = _client(handler)
    return await a.transcribe(model="whisper-1", audio=b"x", filename="a.wav", mimetype="audio/wav")


async def _tts_first(handler):
    t = OpenAITTS(_tts())
    t._client = _client(handler)
    return [item async for item in t.stream(model="tts-1", text="hi", voice="alloy")][0]


@pytest.mark.parametrize("run", [_asr, _tts_first], ids=["asr", "tts"])
async def test_the_configured_key_is_masked_in_the_error_text(run):
    out = await run(_echo)
    assert isinstance(out, SpeechError) and KEY not in out.message, out.message


@pytest.mark.parametrize("run", [_asr, _tts_first], ids=["asr", "tts"])
async def test_the_error_text_is_capped(run):
    out = await run(_page)
    assert isinstance(out, SpeechError) and len(out.message) <= 4100, len(out.message)


@pytest.mark.parametrize("run", [_asr, _tts_first], ids=["asr", "tts"])
async def test_code_and_retriable_are_unchanged(run):
    out = await run(_echo)
    assert out.code == "BadRequestError" and out.retriable is False
