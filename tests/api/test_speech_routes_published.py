"""The speech routers are mounted (S4 P1 Task 16).

A router that is written but never added in _app_routes.py fails nowhere
else in P1: every other unit test imports its module directly. The
OpenAPI document off the assembled app is the cheapest place the mount
itself is observable.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_the_speech_crud_and_helper_routes_are_published(app: FastAPI) -> None:
    paths = set(app.openapi()["paths"])
    assert "/v1/stt_providers" in paths
    assert "/v1/tts_providers" in paths
    # The helper routers must be mounted BEFORE their CRUD siblings or
    # GET /{provider_id} swallows the literal (Task 10).
    assert "/v1/stt_providers/_test" in paths
    assert "/v1/stt_providers/_types" in paths
    assert "/v1/tts_providers/_test" in paths
    assert "/v1/tts_providers/_types" in paths


@pytest.mark.asyncio
async def test_the_audio_and_active_config_routes_are_published(app: FastAPI) -> None:
    paths = set(app.openapi()["paths"])
    assert "/v1/speech_active_config" in paths
    assert "/v1/audio/transcriptions" in paths
    assert "/v1/audio/speech" in paths
    assert "/v1/audio/models" in paths
    assert "/v1/audio/voices" in paths
