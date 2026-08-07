"""A named (provider, model) pair plus its API-level configuration.

One model may be registered many times under a single provider with
different configuration, and the profile id is what an
:class:`~primer.model.agent.Agent` references. That is the whole point of
the entity: before it existed, ``LLMProvider.models[]`` was keyed by the
provider-side wire name, so the same model could not be registered twice
with, say, reasoning on and reasoning off.

This entity replaces ``LLMProvider.models[]`` as the sole registry of what
a provider can serve; ``context_length`` moved here from the former
``LLMModel``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, Field, PositiveInt

from primer.model.common import Describeable


class ReasoningLevel(str, Enum):
    """Vendor-neutral reasoning effort, mapped per adapter.

    No two vendors expose the same scale, so each adapter maps these onto
    the closest wire value it has and documents the approximation. This is
    the same normalisation the codebase already does for stop reasons: the
    universal vocabulary is the contract, and the translation lives at the
    SDK boundary.

    ``OFF`` means "suppress reasoning wherever the vendor allows it". Some
    vendors have no true off switch (the OpenAI Responses API's floor is
    ``minimal``), so ``OFF`` is a request rather than a guarantee.
    """

    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ModelProfileConfig(BaseModel):
    """API-level tunables applied to every call made under a profile.

    Deliberately does NOT carry ``temperature`` or ``max_output_tokens``:
    those live on :class:`~primer.model.agent.Agent`, and duplicating them
    here would create a precedence question with no defensible answer. This
    block is scoped to model-BEHAVIOUR tunables only.

    An empty config is inert: a profile carrying it behaves exactly as a
    bare ``(provider, model)`` pair did before profiles existed.
    """

    reasoning: ReasoningLevel | None = Field(
        default=None,
        description=(
            "Reasoning effort for this profile. None defers to the vendor "
            "default. Mapped per adapter onto the vendor's own wire shape."
        ),
    )
    extended: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Provider-specific knobs forwarded verbatim to "
            "``LLM.stream(extended=...)``. Adapters whitelist the keys they "
            "understand and DEBUG-log the remainder, so an unrecognised key "
            "is inert rather than an error. Use this for anything not worth "
            "normalising across vendors."
        ),
    )


class ModelProfile(Describeable):
    """One registered configuration of one model on one provider."""

    _id_prefix: ClassVar[str] = "model-profile"

    provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the :class:`~primer.model.provider.LLMProvider` "
            "this profile draws from."
        ),
    )
    model_name: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-side wire name (e.g. 'gpt-4o-mini', 'qwen'). Several "
            "profiles on one provider may share a model_name; they are "
            "distinguished by their id and config."
        ),
    )
    context_length: PositiveInt = Field(
        ...,
        description=(
            "Maximum tokens the model accepts in a request. Consumed by "
            "compaction to decide when to summarise."
        ),
    )
    config: ModelProfileConfig = Field(
        default_factory=ModelProfileConfig,
        description="API-level tunables applied to every call under this profile.",
    )
    harness_id: str | None = Field(
        default=None,
        description=(
            "Set when this row is managed by a harness; direct CRUD is then "
            "rejected with 409 and the row changes only by re-running the "
            "harness."
        ),
    )


__all__ = ["ModelProfile", "ModelProfileConfig", "ReasoningLevel"]
