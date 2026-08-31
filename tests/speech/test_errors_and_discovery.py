"""Speech error mapping + OpenAI-compatible enumeration (S4 P1 Task 5)."""

from __future__ import annotations

import httpx
import pytest

from primer.model.speech import SpeechError, WARMING_UP_CODE
from primer.speech._errors import speech_error_from_exception
from primer.speech.discovery import list_models, list_voices


class _Status(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_503_maps_to_a_retriable_warming_up_error() -> None:
    err = speech_error_from_exception(_Status(503))
    assert isinstance(err, SpeechError)
    assert err.code == WARMING_UP_CODE
    assert err.retriable is True


def test_other_failures_are_not_retriable() -> None:
    err = speech_error_from_exception(_Status(401))
    assert err.retriable is False
    assert err.code != WARMING_UP_CODE


def test_a_plain_exception_still_produces_an_error_value() -> None:
    err = speech_error_from_exception(RuntimeError("boom"))
    assert isinstance(err, SpeechError)
    assert "boom" in err.message


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_list_models_reads_the_openai_data_array(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "whisper-1"}, {"id": "kokoro"}]})

    monkeypatch.setattr(
        "primer.speech.discovery._transport_for_tests", _transport(_handler),
    )
    out = await list_models(url="http://asr.local:8006/v1", api_key="sk-x")
    assert out == ["whisper-1", "kokoro"]
    assert seen["url"] == "http://asr.local:8006/v1/models"
    assert seen["auth"] == "Bearer sk-x"


@pytest.mark.asyncio
async def test_list_models_sends_no_auth_header_without_a_key(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(
        "primer.speech.discovery._transport_for_tests", _transport(_handler),
    )
    assert await list_models(url="http://asr.local:8006/v1", api_key=None) == []
    assert seen["auth"] is None


@pytest.mark.asyncio
async def test_list_voices_prefers_the_audio_voices_route(monkeypatch) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/audio/voices")
        return httpx.Response(200, json={"voices": ["af_heart", "am_adam"]})

    monkeypatch.setattr(
        "primer.speech.discovery._transport_for_tests", _transport(_handler),
    )
    assert await list_voices(url="http://tts.local:8004/v1", api_key=None) == [
        "af_heart",
        "am_adam",
    ]


@pytest.mark.asyncio
async def test_list_voices_falls_back_to_models_when_voices_is_absent(monkeypatch) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/audio/voices"):
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={"data": [{"id": "tts-1"}]})

    monkeypatch.setattr(
        "primer.speech.discovery._transport_for_tests", _transport(_handler),
    )
    assert await list_voices(url="http://tts.local:8004/v1", api_key=None) == ["tts-1"]
