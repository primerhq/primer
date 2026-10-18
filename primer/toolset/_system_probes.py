"""System tools that probe a provider before it is registered: the Hugging Face Hub model check.

Split out of :mod:`primer.toolset.system` like the other ``_system_*`` modules. These tools take no per-build dependency (they read no storage and no registry: they ask
the Hub or the provider's own endpoint and store nothing), so :func:`probe_tools` builds them standalone and ``build_system_toolset`` registers the result.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from primer.common.hf_hub import check_hf_models
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
        max_length=50,
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


def probe_tools() -> dict[str, tuple[Tool, ToolHandler]]:
    """The probe tools, keyed by tool id, ready for ``build_system_toolset`` to register."""
    name, entry = _check_huggingface_models_tool()
    return {name: entry}
