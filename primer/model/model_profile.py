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

from enum import Enum, StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, PositiveInt, model_validator

from primer.model.common import Describeable


class RoutingStrategy(StrEnum):
    """How an aggregated profile picks the member to try first."""

    SEQUENTIAL = "sequential"      # fixed priority queue: always start at members[0]
    ROUND_ROBIN = "round_robin"    # rotate the start position once per stream call


class FailoverPoint(StrEnum):
    """When an aggregated profile is still allowed to fail over."""

    BEFORE_FIRST_TOKEN = "before_first_token"  # default; never re-emits tokens
    MID_STREAM = "mid_stream"                  # opt-in; may duplicate already-shown tokens


class FailoverClasses(StrEnum):
    """Which error classes are eligible to trigger failover."""

    TRANSIENT = "transient"                        # rate-limit / 5xx / timeout / network
    TRANSIENT_AND_CONFIG = "transient_and_config"  # + auth / bad-request (mirrors web-search)


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
    """One registered configuration of one model on one provider, OR an
    ordered aggregation of two or more other ModelProfiles.

    ``kind`` discriminates the two shapes on this ONE entity (no wrapper
    or nested union — every existing ``profile.provider_id`` read stays
    valid for the common "single" case): a "single" profile is the
    original (provider, model) pair; an "aggregated" profile carries no
    provider/model of its own and instead names an ordered pool of member
    profile ids to fail over across (see :mod:`primer.model_profile` for
    the resolver, :class:`primer.llm.aggregated.AggregatedLLM` for the
    adapter). Member-content rules that need row lookups (existence, that
    every member is itself "single", no self-reference, no duplicates,
    minimum size) live on the ``model_profiles`` router's CRUD hooks, not
    here — this model only enforces the shape a bare Pydantic validator
    can see.
    """

    _id_prefix: ClassVar[str] = "model-profile"

    kind: Literal["single", "aggregated"] = Field(
        default="single",
        description=(
            "\"single\": provider_id/model_name/context_length name the "
            "model this profile serves. \"aggregated\": this profile IS "
            "an ordered pool of >= 2 other ModelProfiles (see members); "
            "provider_id/model_name/context_length are null."
        ),
    )
    provider_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Identifier of the :class:`~primer.model.provider.LLMProvider` "
            "this profile draws from. Required when kind=\"single\"; null "
            "when kind=\"aggregated\"."
        ),
    )
    model_name: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Provider-side wire name (e.g. 'gpt-4o-mini', 'qwen'). Several "
            "profiles on one provider may share a model_name; they are "
            "distinguished by their id and config. Required when kind="
            "\"single\"; null when kind=\"aggregated\"."
        ),
    )
    context_length: PositiveInt | None = Field(
        default=None,
        description=(
            "Maximum tokens the model accepts in a request. Consumed by "
            "compaction to decide when to summarise. Required when kind="
            "\"single\". Null on the stored row when kind=\"aggregated\" — "
            "resolve_model reports the MIN over the resolved member "
            "context lengths instead, so a caller never overpromises the "
            "window."
        ),
    )
    members: list[str] | None = Field(
        default=None,
        description=(
            "Ordered ModelProfile ids this aggregated profile fails over "
            "across — order IS the routing/failover chain, so it is "
            "preserved verbatim (never sorted or deduped here). Null when "
            "kind=\"single\"."
        ),
    )
    strategy: RoutingStrategy = Field(
        default=RoutingStrategy.SEQUENTIAL,
        description="Meaningful only when kind=\"aggregated\".",
    )
    failover_point: FailoverPoint = Field(
        default=FailoverPoint.BEFORE_FIRST_TOKEN,
        description="Meaningful only when kind=\"aggregated\".",
    )
    failover_on: FailoverClasses = Field(
        default=FailoverClasses.TRANSIENT_AND_CONFIG,
        description="Meaningful only when kind=\"aggregated\".",
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

    @model_validator(mode="after")
    def _check_kind_shape(self) -> "ModelProfile":
        """Required-fields-by-kind, mirroring LLMProvider's own
        provider-to-config coercion validator, inverted: there the
        discriminator picks which config class is valid; here it picks
        which of two field groups must be populated on this one class.
        """
        if self.kind == "single":
            missing = [
                name
                for name in ("provider_id", "model_name", "context_length")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"kind='single' requires {', '.join(missing)} to be set"
                )
            if self.members is not None:
                raise ValueError("kind='single' must not set members")
        else:  # "aggregated"
            if any(
                getattr(self, name) is not None
                for name in ("provider_id", "model_name", "context_length")
            ):
                raise ValueError(
                    "kind='aggregated' must not set provider_id/model_name/"
                    "context_length"
                )
        return self


__all__ = [
    "FailoverClasses",
    "FailoverPoint",
    "ModelProfile",
    "ModelProfileConfig",
    "ReasoningLevel",
    "RoutingStrategy",
]
