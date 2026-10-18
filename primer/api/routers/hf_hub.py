"""``POST /v1/huggingface/_check_models``: can this token pull these Hugging Face models?

The Register provider overlay asks it on the way from a Hugging Face embedding or cross-encoder provider's first page to its second, and an agent asks it through the
``check_huggingface_models`` system tool, before the provider is created: each model is looked up on the Hub (``HF_ENDPOINT``, the mirror the models will be pulled from) with the
token, and the answer says which exist and which the token can pull (``primer/common/hf_hub.py`` holds the mapping of the Hub's answers). It stores nothing and the token is
used for the requests and for nothing else.

Mounted with the admin gate in ``_app_routes.py``: it spends the deployment's network on the caller's behalf, and the token is a credential.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from primer.api.errors import common_responses
from primer.common.hf_hub import MAX_MODELS_PER_CHECK, HfModelCheck, check_hf_models

hf_hub_router = APIRouter(tags=["providers"])


class _HfCheckBody(BaseModel):
    models: list[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_MODELS_PER_CHECK,
        description="Model names to look up, as `namespace/name` (or a bare `name`). The answer lists them in this order.",
    )
    token: str | None = Field(
        default=None,
        description="Hugging Face token for the requests. Leave out for public models; a gated or private model needs the token of an account that can read it.",
    )


class _HfCheckAnswer(BaseModel):
    models: list[HfModelCheck] = Field(..., description="One verdict per requested model, in the order requested.")


@hf_hub_router.post(
    "/huggingface/_check_models",
    response_model=_HfCheckAnswer,
    responses=common_responses(422),
    summary=(
        "Check, before registering a Hugging Face provider, that each model exists on the Hub and that the token can pull it. "
        "Stores nothing; a model the Hub refuses is a result in the 200 answer."
    ),
)
async def check_huggingface_models_route(body: _HfCheckBody) -> _HfCheckAnswer:
    return _HfCheckAnswer(models=await check_hf_models(body.models, token=body.token))
