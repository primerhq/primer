"""``GET /v1/agents/{id}/status`` WARNS about an output cap that fills the model's window (01a10c6b item 2).

With ``max_output_tokens >= context_length`` no prompt fits beside the cap, so a provider that checks the cap rejects
every request. The agent still SAVES (some servers clamp an oversized cap instead of rejecting it, so a refusal would
break setups that work), and the guard in the executor is reactive (``output_cap_never_fits``): the first call, and any
tool rounds before it, are spent before it acts. The status endpoint is the place the console already reads after a
save, so it carries a ``warnings`` list next to ``issues``: a warning does not make ``ok`` false.

For an AGGREGATED profile the check uses the LARGEST member window (a cap between the windows can still be taken by the
larger member), so it warns only when no member could ever take the call.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from primer.model.agent import Agent, AgentModel
from primer.model.model_profile import ModelProfile
from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType


async def _seed(fake_storage_provider, *windows: int, aggregated: bool = False) -> str:
    """One provider and one profile per window; returns the id of the profile the agent should name."""
    await fake_storage_provider.get_storage(LLMProvider).create(
        LLMProvider(
            id="prov-cap", provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("x")),
            limits=Limits(max_concurrency=4),
        )
    )
    profiles = fake_storage_provider.get_storage(ModelProfile)
    ids = []
    for i, window in enumerate(windows):
        ids.append(f"cap-mem-{i}")
        await profiles.create(
            ModelProfile(
                id=ids[-1], description="member", provider_id="prov-cap", model_name="m", context_length=window,
            )
        )
    if not aggregated:
        return ids[0]
    await profiles.create(ModelProfile(id="cap-agg", description="aggregate", kind="aggregated", members=ids))
    return "cap-agg"


async def _status(client, profile_id: str, cap: int | None) -> dict:
    agent = Agent(
        id="agt-cap", description="x", model=AgentModel(profile_id=profile_id), tools=[], system_prompt=["x"],
        max_output_tokens=cap,
    )
    assert (await client.post("/v1/agents", json=agent.model_dump(mode="json"))).status_code == 201, "the agent must save"
    resp = await client.get("/v1/agents/agt-cap/status")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_a_cap_that_fills_the_window_is_a_warning_not_an_issue(client, fake_storage_provider) -> None:
    profile = await _seed(fake_storage_provider, 4096)

    body = await _status(client, profile, 4096)

    assert body["ok"] is True and body["issues"] == [], "a warning must not make the agent unusable"
    warnings = body.get("warnings")
    assert warnings and len(warnings) == 1, f"no warning for a cap equal to the window: {body}"
    assert "max_output_tokens (4096)" in warnings[0] and "context window (4096)" in warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1024, 4095, None], ids=["well-below", "one-below", "unset"])
async def test_a_cap_below_the_window_or_unset_warns_of_nothing(client, fake_storage_provider, cap) -> None:
    profile = await _seed(fake_storage_provider, 4096)

    body = await _status(client, profile, cap)

    assert body.get("warnings") == []


@pytest.mark.asyncio
async def test_an_aggregated_cap_between_the_windows_warns_of_nothing(client, fake_storage_provider) -> None:
    """8192 < 10000 < 32768: the larger member can take it (the executor's guard reads the MIN window, see #434)."""
    profile = await _seed(fake_storage_provider, 8192, 32768, aggregated=True)

    body = await _status(client, profile, 10_000)

    assert body["ok"] is True and body.get("warnings") == []


@pytest.mark.asyncio
async def test_an_aggregated_cap_above_every_window_warns_with_the_largest(client, fake_storage_provider) -> None:
    profile = await _seed(fake_storage_provider, 8192, 32768, aggregated=True)

    body = await _status(client, profile, 40_000)

    warnings = body.get("warnings")
    assert warnings and len(warnings) == 1, f"no warning for a cap above every member window: {body}"
    assert "max_output_tokens (40000)" in warnings[0] and "context window (32768)" in warnings[0]
