"""The whole 422 body the agent create / update reference checks answer with (A-09, the create/update half).

``tests/api/test_agent_reference_checks.py`` asserts the status, the code and the wording. This pins the WHOLE response of each refusal:
status, content type and the raw ``application/problem+json`` body (the ``request_id`` normalised), so a change to the envelope, the
``extensions`` keys or the order of the keys is seen. The bodies did not exist before the check did, so ``GOLDEN`` was captured from the
first green run of these exact requests.
"""

from __future__ import annotations

import re

import pytest

from primer.model.agent import Agent
from tests._support.agent_check_rows import agent_body, profile_row
from tests.api.conftest import app, client, fake_provider_registry  # noqa: F401

PROFILE_MESSAGE = "ModelProfile 'ghost-profile' does not exist; create the profile first or name an existing one"
TOOLSETS_MESSAGE = "tools name toolsets that do not exist: 'alpha', 'zeta'; create them first or remove those tools"


def _body(instance: str, error: str, field: str, message: str) -> str:
    return (
        '{"type":"/errors/validation-error","title":"Validation Error","status":422,'
        f'"detail":"{message}","instance":"{instance}",'
        f'"extensions":{{"error":"{error}","field":"{field}","message":"{message}","request_id":"req-<id>"}}}}'
    )


GOLDEN: dict[str, tuple[int, str, str]] = {
    "create_missing_profile": (
        422,
        "application/problem+json",
        _body("/v1/agents", "model_profile_not_found", "model.profile_id", PROFILE_MESSAGE),
    ),
    "create_missing_toolsets": (
        422,
        "application/problem+json",
        _body("/v1/agents", "toolset_not_found", "tools", TOOLSETS_MESSAGE),
    ),
    "update_missing_profile": (
        422,
        "application/problem+json",
        _body("/v1/agents/ag-1", "model_profile_not_found", "model.profile_id", PROFILE_MESSAGE),
    ),
    "update_added_missing_toolsets": (
        422,
        "application/problem+json",
        _body("/v1/agents/ag-1", "toolset_not_found", "tools", TOOLSETS_MESSAGE),
    ),
}


def _normalised(response) -> tuple[int, str, str]:
    return response.status_code, response.headers["content-type"], re.sub(r"req-[0-9a-f]+", "req-<id>", response.text)


async def _seed_agent(app) -> None:
    storage = app.state.storage_provider
    await storage.get_storage(Agent).create(Agent.model_validate(agent_body("ag-1")))


@pytest.mark.asyncio
async def test_the_create_refusals_have_these_exact_bodies(client, app) -> None:
    await app.state.storage_provider.get_storage(type(profile_row("mp-1"))).create(profile_row("mp-1"))

    missing_profile = await client.post("/v1/agents", json=agent_body("ag-1", profile_id="ghost-profile"))
    missing_toolsets = await client.post("/v1/agents", json=agent_body("ag-1", tools=["zeta__a", "alpha__b", "zeta__c"]))

    assert _normalised(missing_profile) == GOLDEN["create_missing_profile"]
    assert _normalised(missing_toolsets) == GOLDEN["create_missing_toolsets"]


@pytest.mark.asyncio
async def test_the_update_refusals_have_these_exact_bodies(client, app) -> None:
    await app.state.storage_provider.get_storage(type(profile_row("mp-1"))).create(profile_row("mp-1"))
    await _seed_agent(app)

    missing_profile = await client.put("/v1/agents/ag-1", json=agent_body("ag-1", profile_id="ghost-profile"))
    added_toolsets = await client.put("/v1/agents/ag-1", json=agent_body("ag-1", tools=["zeta__a", "alpha__b"]))

    assert _normalised(missing_profile) == GOLDEN["update_missing_profile"]
    assert _normalised(added_toolsets) == GOLDEN["update_added_missing_toolsets"]
