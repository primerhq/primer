"""System tools that probe a provider before it is registered: the Hugging Face Hub model check and the draft tests of embedding and cross-encoder providers.

Split out of :mod:`primer.toolset.system` like the other ``_system_*`` modules. These tools take no per-build dependency (they read no storage and no registry: they ask
the Hub or the provider's own endpoint and store nothing), so :func:`probe_tools` builds them standalone and ``build_system_toolset`` registers the result.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from primer.common.hf_hub import MAX_MODELS_PER_CHECK, check_hf_models
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.toolset._describe import make_tool
from primer.toolset._helpers import ok as _ok
from primer.toolset._system_common import SYSTEM_TOOLSET_ID, _err_from_validation
from primer.toolset.internal import ToolHandler


class _CheckHuggingFaceArgs(BaseModel):
    """Arguments of ``check_huggingface_models`` (the body of ``POST /v1/huggingface/_check_models``)."""

    models: list[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_MODELS_PER_CHECK,
        description="Model names to look up on the Hub, as `namespace/name` (or a bare `name`), 1 to 50. The result lists them in this order.",
    )
    token: str | None = Field(
        default=None,
        description="Hugging Face token to check with. Leave out for public models; a gated or private model needs the token of an account that can read it.",
    )


def _check_huggingface_models_tool() -> tuple[str, tuple[Tool, ToolHandler]]:
    async def _handler(arguments: dict) -> ToolCallResult:
        try:
            args = _CheckHuggingFaceArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        results = await check_hf_models(args.models, token=args.token)
        return _ok({"models": [check.model_dump() for check in results]})

    name = "check_huggingface_models"
    return name, (
        make_tool(
            id=name,
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Check that Hugging Face Hub models exist and that a token can pull them.",
            when=(
                "Use when you are about to register a Hugging Face embedding or cross-encoder provider and want to confirm that each model exists and that the token can "
                "pull it. It stores nothing. It asks the Hub (or the mirror named by HF_ENDPOINT) once per model and returns ``{models: [{name, ok, status, detail}]}`` in the "
                "order given; ``status`` is one of ok, gated, not_found, bad_token, disabled, invalid_name or error, and a model the Hub refuses is a result, not a tool error."
            ),
            args_schema=_CheckHuggingFaceArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"models": ["BAAI/bge-small-en-v1.5", "BAAI/bge-reranker-v2-m3"]},
                    returns="``{models: [{name, ok, status, detail}, ...]}``, one entry per model",
                ),
                ToolExample(
                    args={"models": ["org/gated-model"], "token": "hf_xxxxxxxx"},
                    returns="``status`` is ``gated`` until the token's account has accepted the model's terms",
                ),
            ],
            required_role="admin",
        ),
        _handler,
    )


class _DraftTestArgs(BaseModel):
    """Arguments of ``test_embedding_provider`` and ``test_cross_encoder_provider``: the draft row, every key optional (the body of the ``_test`` routes)."""

    provider: str | None = Field(
        default=None,
        description="The provider kind of the draft. Embedding: `openai`, `gemini` or `huggingface`. Cross encoder: `huggingface` (the default, there is no other).",
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The kind's connection config, as it would be saved: `{url, api_key?}` for `openai`, `{api_key?}` for `gemini`, `{token?}` for `huggingface`. "
            "A missing config is an empty one."
        ),
    )
    models: list[Any] | None = Field(
        default=None,
        description=(
            "The models the provider would carry, as rows: `[{name}]` for an embedding provider, `[{name, max_pair_length?}]` for a cross encoder. "
            "A `huggingface` draft needs at least one (1 to 50): each is looked up on the Hub. `openai` and `gemini` ignore their content."
        ),
    )


async def _test_embedding_provider_handler(arguments: dict[str, Any]) -> ToolCallResult:
    try:
        args = _DraftTestArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)
    # Imported here: the API router imports the registries, and the registries import the toolsets.
    from primer.api.routers.providers import embedding_draft_test

    return _ok(await embedding_draft_test(args.model_dump(exclude_none=True)))


async def _test_cross_encoder_provider_handler(arguments: dict[str, Any]) -> ToolCallResult:
    try:
        args = _DraftTestArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)
    from primer.api.routers.providers import cross_encoder_draft_test

    return _ok(await cross_encoder_draft_test(args.model_dump(exclude_none=True)))


def _test_embedding_provider_tool() -> tuple[str, tuple[Tool, ToolHandler]]:
    name = "test_embedding_provider"
    return name, (
        make_tool(
            id=name,
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Test a draft embedding provider before creating it: list the models its server offers, or check Hugging Face models on the Hub.",
            when=(
                "Use when you are about to create an embedding provider and want to know whether it would work. It stores nothing. ``openai`` lists the models the server "
                "answers on ``<url>/models`` (chat models included) and ``gemini`` lists Google's embedContent models: ``{ok: true, models: [names]}``. ``huggingface`` checks "
                "each model in ``models`` on the Hub with ``config.token``: ``{ok, models: [names that passed], checks: [{name, ok, status, detail}], error?}``. A draft that "
                "cannot be a provider row is ``{ok: false, error: 'invalid draft: ...'}`` and an upstream failure is ``{ok: false, error}``: both are answers, not tool errors. "
                "``id`` and ``limits`` are ignored."
            ),
            args_schema=_DraftTestArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"provider": "openai", "config": {"url": "http://localhost:1234/v1"}},
                    returns="``{ok: true, models: [...]}``, the model names the server lists",
                ),
                ToolExample(
                    args={"provider": "gemini", "config": {"api_key": "AIza-xxxxxxxx"}},
                    returns="``{ok: true, models: ['gemini-embedding-001', ...]}``",
                ),
                ToolExample(
                    args={"provider": "huggingface", "config": {}, "models": [{"name": "BAAI/bge-small-en-v1.5"}]},
                    returns="``{ok, models, checks}``; ``ok`` is false, with ``error`` naming them, when a model fails the Hub check",
                ),
            ],
            required_role="admin",
        ),
        _test_embedding_provider_handler,
    )


def _test_cross_encoder_provider_tool() -> tuple[str, tuple[Tool, ToolHandler]]:
    name = "test_cross_encoder_provider"
    return name, (
        make_tool(
            id=name,
            toolset_id=SYSTEM_TOOLSET_ID,
            purpose="Test a draft cross-encoder provider before creating it: check its Hugging Face models on the Hub.",
            when=(
                "Use when you are about to create a cross-encoder (reranker) provider and want to know whether each of its models exists and can be pulled with the token. "
                "It stores nothing. Answers ``{ok, models: [names that passed], checks: [{name, ok, status, detail}], error?}``; ``ok`` is false, with ``error`` naming them, "
                "when a model fails the Hub check. A draft with no model, or one that cannot be a provider row, is ``{ok: false, error: 'invalid draft: ...'}``: an answer, "
                "not a tool error. ``provider`` may be left out (``huggingface`` is the only kind); ``id`` and ``limits`` are ignored."
            ),
            args_schema=_DraftTestArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"config": {}, "models": [{"name": "BAAI/bge-reranker-v2-m3", "max_pair_length": 512}]},
                    returns="``{ok, models, checks}``",
                ),
            ],
            required_role="admin",
        ),
        _test_cross_encoder_provider_handler,
    )


def probe_tools() -> dict[str, tuple[Tool, ToolHandler]]:
    """The probe tools, keyed by tool id, ready for ``build_system_toolset`` to register."""
    return dict(
        [
            _check_huggingface_models_tool(),
            _test_embedding_provider_tool(),
            _test_cross_encoder_provider_tool(),
        ]
    )
