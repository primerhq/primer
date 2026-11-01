"""D21 / GI-26: generous caps on the free text a user writes (description 16 KiB, prompts 256 KiB); an unchanged over-cap field still saves."""

from __future__ import annotations

from primer.common.text_caps import DESCRIPTION_MAX_CHARS, PROMPT_MAX_CHARS, check_text_caps, oversized_fields
from primer.common.entity_checks import EntityCheckError
from primer.model.agent import Agent
from tests._support.model_profiles import seed_profile
from tests.api.test_model_profiles_router import _agent, _seed_provider

import pytest


async def _profile(client) -> str:
    await _seed_provider(client, "cap-prov")
    return await seed_profile(client, "cap-prov", "cap-model")


async def test_an_over_cap_description_answers_422_and_the_cap_itself_passes(client) -> None:
    pid = await _profile(client)
    ok = _agent("cap-ok", pid) | {"description": "d" * DESCRIPTION_MAX_CHARS}
    assert (await client.post("/v1/agents", json=ok)).status_code == 201
    too_long = _agent("cap-big", pid) | {"description": "d" * (DESCRIPTION_MAX_CHARS + 1)}
    r = await client.post("/v1/agents", json=too_long)
    assert r.status_code == 422 and "description" in r.text, r.text


async def test_an_over_cap_prompt_answers_422(client) -> None:
    pid = await _profile(client)
    body = _agent("cap-prompt", pid) | {"system_prompt": ["p" * (PROMPT_MAX_CHARS // 2), "p" * (PROMPT_MAX_CHARS // 2 + 1)]}
    r = await client.post("/v1/agents", json=body)
    assert r.status_code == 422 and "system_prompt" in r.text, r.text


async def test_a_save_that_leaves_an_over_cap_field_unchanged_still_passes(client, app) -> None:
    pid = await _profile(client)
    big = "d" * (DESCRIPTION_MAX_CHARS + 500)
    row = Agent.model_validate(_agent("cap-old", pid) | {"description": big})
    await app.state.storage_provider.get_storage(Agent).create(row)  # stored before the cap (an internal writer)

    unchanged = await client.put("/v1/agents/cap-old", json=_agent("cap-old", pid) | {"description": big, "system_prompt": ["new"]})
    assert unchanged.status_code == 200, unchanged.text
    grown = await client.put("/v1/agents/cap-old", json=_agent("cap-old", pid) | {"description": big + "x"})
    assert grown.status_code == 422, grown.text


def test_the_check_names_the_first_over_cap_field() -> None:
    entity = type("E", (), {"description": "x" * (DESCRIPTION_MAX_CHARS + 1), "name": "n", "system_prompt": None})()
    assert [f for f, _, _ in oversized_fields(entity)] == ["description"]
    with pytest.raises(EntityCheckError) as caught:
        check_text_caps(entity)
    assert caught.value.field == "description"
    assert oversized_fields(entity, type("S", (), {"description": entity.description})()) == []
