"""REST routes for the speech (ASR / TTS) provider subsystem.

Five routers mounted under /v1/:

* ``stt_providers_helpers_router`` / ``tts_providers_helpers_router`` --
  the ``_test`` and ``_types`` helpers. These MUST be included BEFORE
  their CRUD siblings so the literal paths win over ``/{id}``.
* ``stt_providers_router`` / ``tts_providers_router`` -- generic CRUD
  built by :func:`make_crud_router`, with registry-invalidation hooks and
  a cascade-block that refuses to delete a row the active speech config
  still points at.
* ``speech_active_config_router`` -- singleton GET / PUT for
  :class:`primer.model.speech.ActiveSpeechConfig`.

Unlike web search, the GET on the singleton NEVER reports 503. Web search
bootstraps a reserved DuckDuckGo row, so a missing singleton there means
bootstrap failed; speech has no reserved provider and an install with no
speech configured is a normal steady state, so the GET synthesises an
empty row instead.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from primer.api.errors import common_responses
from primer.api.routers._crud import make_crud_router, preserve_masked_secrets_on_update
from primer.model.provider import SpeechToTextProvider, TextToSpeechProvider
from primer.model.speech import ACTIVE_SPEECH_CONFIG_ID, ActiveSpeechConfig
from primer.speech.discovery import list_models, list_voices


logger = logging.getLogger(__name__)


# ---------- Storage deps -----------------------------------------


def _stt_storage(request: Request):
    return request.app.state.storage_provider.get_storage(SpeechToTextProvider)


def _tts_storage(request: Request):
    return request.app.state.storage_provider.get_storage(TextToSpeechProvider)


def _active_speech_storage(request: Request):
    return request.app.state.storage_provider.get_storage(ActiveSpeechConfig)


async def read_active_speech_config(request: Request) -> ActiveSpeechConfig:
    """Read the singleton, synthesising an empty row when it is absent.

    Shared with the audio proxy router so both surfaces agree on what
    "nothing configured" looks like.
    """
    row = await _active_speech_storage(request).get(ACTIVE_SPEECH_CONFIG_ID)
    return row or ActiveSpeechConfig(id=ACTIVE_SPEECH_CONFIG_ID)


# ---------- Cascade-block on delete ------------------------------


def _cascade_blocker(field: str):
    """Build an on_pre_delete_id hook guarding one active-config field."""

    async def _hook(entity_id: str, request: Request) -> None:
        active = await read_active_speech_config(request)
        if getattr(active, field) != entity_id:
            return
        raise HTTPException(
            status_code=409,
            detail={
                "error": "cascade_blocked",
                "message": (
                    "currently referenced by the active speech config; "
                    "update the active config first"
                ),
                "referenced_by": ACTIVE_SPEECH_CONFIG_ID,
            },
        )

    return _hook


# ---------- Registry invalidation hooks --------------------------


def _invalidator(attr: str):
    """Build an on_update / on_delete hook for one registry."""

    async def _hook(entity_id: str, request: Request) -> None:
        registry = getattr(request.app.state, attr, None)
        if registry is not None:
            await registry.invalidate(entity_id)

    return _hook


# ---------- _test and _types helpers (mounted BEFORE CRUD) -------


class _SttDraft(BaseModel):
    """A half-filled form, deliberately all-optional.

    The point of ``_test`` is to answer a draft the console has not
    finished typing. Requiring the fields here would make FastAPI reject
    it with a 422 before the handler could report WHICH field is missing,
    so validation happens inside against the real row model.
    """

    id: str | None = None
    provider: str | None = None
    default_model: str | None = None
    config: Any = None
    limits: Any = None


class _TtsDraft(BaseModel):
    """See :class:`_SttDraft`: all-optional so a partial draft is
    reported rather than rejected."""

    id: str | None = None
    provider: str | None = None
    default_model: str | None = None
    default_voice: str | None = None
    config: Any = None
    limits: Any = None


def _api_key_of(config) -> str | None:
    secret = getattr(config, "api_key", None)
    return secret.get_secret_value() if secret is not None else None


stt_providers_helpers_router = APIRouter(tags=["speech"])
tts_providers_helpers_router = APIRouter(tags=["speech"])


@stt_providers_helpers_router.post(
    "/stt_providers/_test",
    responses=common_responses(500),
    summary=(
        "Test a draft speech-to-text config with a live GET /models round "
        "trip. Returns {ok, models} or {ok=false, error}."
    ),
)
async def test_stt_provider(body: _SttDraft) -> dict[str, Any]:
    try:
        draft = SpeechToTextProvider.model_validate(body.model_dump())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"invalid draft: {exc}"}
    try:
        models = await list_models(
            url=str(draft.config.url), api_key=_api_key_of(draft.config),
        )
    except Exception as exc:  # noqa: BLE001 -- diagnostic-only path
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "models": models}


@stt_providers_helpers_router.get(
    "/stt_providers/_types",
    summary="Provider-type metadata for the catalog's speech-to-text form.",
)
async def list_stt_types() -> dict[str, dict[str, Any]]:
    return {
        "openai": {
            "label": "OpenAI-compatible",
            # url is REQUIRED on the speech configs; api_key is not.
            # Listed as bare strings the form read both as optional, so
            # it offered Save on a row with no endpoint and the create
            # came back 422.
            "config_fields": [
                {"key": "url", "label": "url", "type": "url",
                 "required": True},
                "api_key",
            ],
            # default_model is REQUIRED on SpeechToTextProvider. Declared
            # as a bare string the form treated it as optional, so Save
            # was offered on an incomplete row and the create came back
            # 422 with nothing on the form having said which field was
            # missing.
            "row_fields": [
                {"key": "default_model", "label": "default_model",
                 "required": True},
            ],
            # No _discover_models route: the model list comes back from
            # POST /stt_providers/_test, which is a live round trip the
            # form already offers.
            "discoverable": False,
            "limits": True,
        },
    }


@tts_providers_helpers_router.post(
    "/tts_providers/_test",
    responses=common_responses(500),
    summary=(
        "Test a draft text-to-speech config with a live GET /audio/voices "
        "round trip (falling back to GET /models). Returns {ok, voices} or "
        "{ok=false, error}."
    ),
)
async def test_tts_provider(body: _TtsDraft) -> dict[str, Any]:
    try:
        draft = TextToSpeechProvider.model_validate(body.model_dump())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"invalid draft: {exc}"}
    try:
        voices = await list_voices(
            url=str(draft.config.url), api_key=_api_key_of(draft.config),
        )
    except Exception as exc:  # noqa: BLE001 -- diagnostic-only path
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "voices": voices}


@tts_providers_helpers_router.get(
    "/tts_providers/_types",
    summary="Provider-type metadata for the catalog's text-to-speech form.",
)
async def list_tts_types() -> dict[str, dict[str, Any]]:
    return {
        "openai": {
            "label": "OpenAI-compatible",
            # url is REQUIRED on the speech configs; api_key is not.
            # Listed as bare strings the form read both as optional, so
            # it offered Save on a row with no endpoint and the create
            # came back 422.
            "config_fields": [
                {"key": "url", "label": "url", "type": "url",
                 "required": True},
                "api_key",
            ],
            # Both are REQUIRED on TextToSpeechProvider; see the note on
            # the STT types above.
            "row_fields": [
                {"key": "default_model", "label": "default_model",
                 "required": True},
                {"key": "default_voice", "label": "default_voice",
                 "required": True},
            ],
            "discoverable": False,
            "limits": True,
        },
    }


# ---------- CRUD routers -----------------------------------------


stt_providers_router = make_crud_router(
    model_cls=SpeechToTextProvider,
    storage_dep=_stt_storage,
    plural="stt_providers",
    tag="speech",
    on_pre_delete_id=_cascade_blocker("stt_provider_id"),
    on_pre_update=preserve_masked_secrets_on_update,
    on_update=_invalidator("stt_registry"),
    on_delete=_invalidator("stt_registry"),
)

tts_providers_router = make_crud_router(
    model_cls=TextToSpeechProvider,
    storage_dep=_tts_storage,
    plural="tts_providers",
    tag="speech",
    on_pre_delete_id=_cascade_blocker("tts_provider_id"),
    on_pre_update=preserve_masked_secrets_on_update,
    on_update=_invalidator("tts_registry"),
    on_delete=_invalidator("tts_registry"),
)


# ---------- Singleton: ActiveSpeechConfig ------------------------


speech_active_config_router = APIRouter(tags=["speech"])


class _ActiveSpeechPutBody(BaseModel):
    stt_provider_id: str | None = None
    tts_provider_id: str | None = None
    tts_voice: str | None = None


async def _validate_referenced_providers(
    row: ActiveSpeechConfig, request: Request,
) -> None:
    """422 naming every referenced provider id that does not exist."""
    unknown: list[str] = []
    if row.stt_provider_id is not None:
        if await _stt_storage(request).get(row.stt_provider_id) is None:
            unknown.append(row.stt_provider_id)
    if row.tts_provider_id is not None:
        if await _tts_storage(request).get(row.tts_provider_id) is None:
            unknown.append(row.tts_provider_id)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown_provider_ids",
                "message": (
                    f"active speech config references unknown provider "
                    f"id(s): {unknown}"
                ),
                "unknown_ids": unknown,
            },
        )


@speech_active_config_router.get(
    "/speech_active_config",
    response_model=ActiveSpeechConfig,
    responses=common_responses(500),
    summary="Read the singleton active speech config",
)
async def get_active_speech_config(request: Request) -> ActiveSpeechConfig:
    return await read_active_speech_config(request)


@speech_active_config_router.put(
    "/speech_active_config",
    response_model=ActiveSpeechConfig,
    responses=common_responses(422, 500),
    summary="Replace the singleton active speech config",
)
async def put_active_speech_config(
    request: Request,
    body: _ActiveSpeechPutBody,
) -> ActiveSpeechConfig:
    try:
        new_row = ActiveSpeechConfig(
            id=ACTIVE_SPEECH_CONFIG_ID,
            stt_provider_id=body.stt_provider_id,
            tts_provider_id=body.tts_provider_id,
            tts_voice=body.tts_voice,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_active_config",
                "message": "active speech config failed validation",
                "errors": exc.errors(include_url=False),
            },
        )
    await _validate_referenced_providers(new_row, request)
    storage = _active_speech_storage(request)
    if await storage.get(ACTIVE_SPEECH_CONFIG_ID) is None:
        await storage.create(new_row)
    else:
        await storage.update(new_row)
    return new_row


__all__ = [
    "read_active_speech_config",
    "speech_active_config_router",
    "stt_providers_helpers_router",
    "stt_providers_router",
    "tts_providers_helpers_router",
    "tts_providers_router",
]
