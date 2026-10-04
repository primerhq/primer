"""LLM.count_tokens_detailed: what a count stands on, through every layer."""

from __future__ import annotations

import pytest

from primer.int.llm import LLM
from primer.llm.aggregated import AggregatedLLM
from primer.llm.openchat import OpenChatLLM
from primer.llm.openresponses import OpenResponsesLLM
from primer.llm.openrouter import OpenRouterLLM
from primer.llm.retrying import RetryingLLM
from primer.model.chat import ImagePart, Message, TextPart
from primer.model.except_ import (
    ConfigError,
    NetworkError,
    TokenCounterUnavailable,
)
from primer.model.media_tokens import IMAGE_TOKENS
from primer.model.model_profile import (
    FailoverClasses,
    FailoverPoint,
    ModelProfile,
    ModelProfileConfig,
    RoutingStrategy,
)
from primer.model.token_count import TokenCount
from primer.model_profile.resolver import ResolvedModel
from tests.llm.test_openchat import _make_provider as chat_provider
from tests.llm.test_openresponses import _make_provider as responses_provider
from tests.llm.test_openrouter import _make_provider as router_provider

TEXT = [Message(role="user", parts=[TextPart(text="hello world")])]


class _CountOnly(LLM):
    """An adapter that has not been taught to say what it counted."""

    async def stream(self, **kwargs):  # pragma: no cover - not used
        raise NotImplementedError

    async def count_tokens(self, *, model, messages, tools=None) -> int:
        return 42


async def test_the_default_claims_nothing():
    detail = await _CountOnly().count_tokens_detailed(model="m", messages=TEXT)
    assert detail == TokenCount(total=42, exact=False, declared=False)


async def test_the_retrying_wrapper_forwards_what_the_inner_adapter_knows():
    inner = _CountOnly()

    async def detailed(**_kw):
        return TokenCount(total=7, exact=True, estimated_components=("media",), encoding="e")

    inner.count_tokens_detailed = detailed  # type: ignore[method-assign]
    wrapper = RetryingLLM.__new__(RetryingLLM)
    wrapper._inner = inner
    got = await wrapper.count_tokens_detailed(model="m", messages=TEXT)
    assert got == TokenCount(total=7, exact=True, estimated_components=("media",), encoding="e")


# ---- the OpenAI family -------------------------------------------------------

FAMILY = [
    pytest.param(lambda m: OpenChatLLM(chat_provider(models=[m])), id="openchat"),
    pytest.param(lambda m: OpenResponsesLLM(responses_provider(models=[m])), id="openresponses"),
    pytest.param(lambda m: OpenRouterLLM(router_provider(models=[m])), id="openrouter"),
]


@pytest.mark.parametrize("make", FAMILY)
async def test_a_model_with_its_own_encoding_is_exact(make):
    detail = await make("gpt-4o").count_tokens_detailed(model="gpt-4o", messages=TEXT)
    assert detail.exact is True
    assert detail.encoding == "o200k_base"
    assert detail.estimated_components == ()


@pytest.mark.parametrize("make", FAMILY)
async def test_an_unknown_model_uses_the_default_encoding_and_says_it_is_approximate(make):
    detail = await make("qwen3-30b").count_tokens_detailed(model="qwen3-30b", messages=TEXT)
    assert detail.exact is False
    assert detail.encoding == "o200k_base"


@pytest.mark.parametrize("make", FAMILY)
async def test_count_tokens_is_the_detailed_total(make):
    llm = make("gpt-4o")
    detail = await llm.count_tokens_detailed(model="gpt-4o", messages=TEXT)
    assert await llm.count_tokens(model="gpt-4o", messages=TEXT) == detail.total


async def test_media_is_a_flat_estimate_reported_as_an_estimated_component():
    llm = OpenChatLLM(chat_provider(models=["gpt-4o"]))
    plain = await llm.count_tokens_detailed(model="gpt-4o", messages=TEXT)
    with_image = await llm.count_tokens_detailed(
        model="gpt-4o",
        messages=[Message(role="user", parts=[
            TextPart(text="hello world"), ImagePart(mime_type="image/png", data=b"\x00"),
        ])],
    )
    assert with_image.estimated_components == ("media",)
    assert IMAGE_TOKENS <= with_image.total - plain.total < IMAGE_TOKENS + 20, (
        "the image adds the shared constant (plus its short marker), not a text filler"
    )


# ---- aggregated --------------------------------------------------------------


class _Member:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    async def count_tokens_detailed(self, *, model, messages, tools=None):
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.behaviour

    async def count_tokens(self, *, model, messages, tools=None) -> int:  # pragma: no cover
        raise AssertionError("the detailed path must be used")


def _aggregate(members: dict[str, object]) -> AggregatedLLM:
    profile = ModelProfile(
        id="agg-1", description="agg", kind="aggregated", members=list(members),
        strategy=RoutingStrategy.SEQUENTIAL,
        failover_point=FailoverPoint.BEFORE_FIRST_TOKEN,
        failover_on=FailoverClasses.TRANSIENT_AND_CONFIG,
    )

    async def resolve(member_id):
        return members[member_id], ResolvedModel(
            profile_id=member_id, provider_id="prov", model_name=f"{member_id}-model",
            context_length=8192, config=ModelProfileConfig(),
        )

    return AggregatedLLM(profile, resolve_member=resolve)


async def test_aggregated_skips_a_member_that_cannot_count_and_never_claims_exact():
    agg = _aggregate({
        "a": _Member(TokenCounterUnavailable("no vocab")),
        "b": _Member(ConfigError("misconfigured")),
        "c": _Member(TokenCount(total=11, exact=True, estimated_components=("media",), encoding="x")),
    })
    detail = await agg.count_tokens_detailed(model="virtual", messages=TEXT)
    assert detail.total == 11
    assert detail.exact is False, "the member that counted may not be the one that serves"
    assert detail.estimated_components == ("media",)


async def test_aggregated_keeps_a_legacy_members_count_undeclared():
    class _Legacy:
        async def count_tokens(self, *, model, messages, tools=None) -> int:
            return 5

    detail = await _aggregate({"a": _Legacy()}).count_tokens_detailed(model="v", messages=TEXT)
    assert (detail.total, detail.exact, detail.declared) == (5, False, False)


async def test_aggregated_surfaces_a_deterministic_rejection_when_every_member_rejected():
    """A 400 caused by this conversation's content is not an unavailable counter:
    the wrapper must see the rejection (reported, never negative-cached)."""
    from primer.model.except_ import BadRequestError

    agg = _aggregate({"a": _Member(BadRequestError("bad")), "b": _Member(BadRequestError("also bad"))})
    with pytest.raises(BadRequestError, match="also bad"):
        await agg.count_tokens_detailed(model="virtual", messages=TEXT)


async def test_aggregated_mixed_rejection_and_unavailable_is_unavailable():
    from primer.model.except_ import BadRequestError

    agg = _aggregate({"a": _Member(BadRequestError("bad")), "b": _Member(TokenCounterUnavailable("none"))})
    with pytest.raises(TokenCounterUnavailable):
        await agg.count_tokens_detailed(model="virtual", messages=TEXT)


async def test_aggregated_with_no_member_that_can_count_raises_unavailable_not_config_error():
    agg = _aggregate({"a": _Member(TokenCounterUnavailable("no vocab")), "b": _Member(ConfigError("x"))})
    with pytest.raises(TokenCounterUnavailable) as exc:
        await agg.count_tokens_detailed(model="virtual", messages=TEXT)
    assert exc.value.transient is False
    assert "no vocab" in str(exc.value)


async def test_aggregated_failure_is_transient_when_any_member_failure_was():
    agg = _aggregate({"a": _Member(NetworkError("down")), "b": _Member(TokenCounterUnavailable("none"))})
    with pytest.raises(TokenCounterUnavailable) as exc:
        await agg.count_tokens_detailed(model="virtual", messages=TEXT)
    assert exc.value.transient is True


async def test_aggregated_with_nothing_resolvable_is_unavailable():
    agg = _aggregate({})
    with pytest.raises(TokenCounterUnavailable, match="no member that can count"):
        await agg.count_tokens(model="virtual", messages=TEXT)


async def test_aggregated_lets_a_programming_error_through_to_the_wrapper():
    agg = _aggregate({"a": _Member(AttributeError("bug")), "b": _Member(TokenCount(total=1, exact=True))})
    with pytest.raises(AttributeError):
        await agg.count_tokens_detailed(model="virtual", messages=TEXT)

