"""GI-20: a new toolset's id must fit a provider tool name (`<id>__<tool>` within 64 of [a-zA-Z0-9_-]); the REST door and the system tool agree."""

from __future__ import annotations

import pytest

from tests.api.test_providers import _toolset
from tests.toolset.test_system_crud_guards import _call
from tests.toolset.test_system_crud_guards import world  # noqa: F401  (the fixture)

BAD_IDS = ["acme.web", "Web Tools", "t" * 2048, "a" * 49, "-lead", "_lead"]


@pytest.mark.parametrize("tid", BAD_IDS)
async def test_rest_create_refuses_an_id_a_provider_would_refuse(client, tid) -> None:
    body = _toolset().model_dump(mode="json") | {"id": tid}
    r = await client.post("/v1/toolsets?allow_unreachable=1", json=body)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("tid", ["acme-web", "web_tools_2", "a", "a" * 48, "0day"])
async def test_rest_create_accepts_a_conforming_id(client, tid) -> None:
    body = _toolset().model_dump(mode="json") | {"id": tid}
    r = await client.post("/v1/toolsets?allow_unreachable=1", json=body)
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("tid", BAD_IDS)
async def test_system_create_toolset_refuses_the_same_ids(world, tid) -> None:  # noqa: F811
    from tests.toolset.test_system import _toolset_body

    _, toolset, _ = world
    is_error, answer = await _call(toolset, "create_toolset", entity=_toolset_body() | {"id": tid})
    assert is_error and answer["type"] == "validation-error", answer


async def test_an_existing_toolset_with_an_old_style_id_still_updates(client, app) -> None:
    from primer.model.provider import Toolset

    body = _toolset().model_dump(mode="json") | {"id": "legacy.web"}
    await app.state.storage_provider.get_storage(Toolset).create(Toolset.model_validate(body))
    r = await client.put("/v1/toolsets/legacy.web?allow_unreachable=1", json=body)
    assert r.status_code == 200, r.text
