"""A trigger slug and a subscription's ``parallelism`` are checked where they come in (ticket 01a16ddb, lead ruling 5).

* A slug outside ``^[a-z][a-z0-9-]{1,63}$`` was checked only by the ``Trigger`` model the service builds, so ``trigger__create`` (and its twin
  ``crud__create_trigger``) raised an unhandled pydantic error and ``POST /v1/triggers`` failed with a server error. Both now refuse it as their
  arguments: ``validation-error`` from a tool, 422 from REST, and nothing is stored.
* ``parallelism`` was typed as any string on every way in: a create with another value raised an unhandled pydantic error, and an update STORED
  it (then read as ``queue``). It is ``skip`` or ``queue`` on create AND update, from the trigger tools, ``system__create_channel_binding`` and
  REST.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from primer.model.storage import OffsetPage
from primer.model.trigger import DelayedTriggerConfig, SessionAppendSubConfig, Subscription, Trigger

BAD_SLUGS = ["Nightly-Digest", "nightly_digest", "9-lives", "has space", "-leading-dash"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fire_at() -> str:
    return (_now() + timedelta(hours=1)).isoformat()


async def _triggers(sp) -> list[Trigger]:
    return (await sp.get_storage(Trigger).list(OffsetPage(offset=0, length=50))).items


async def _seed_trigger_with_sub(sp) -> None:
    await sp.get_storage(Trigger).create(Trigger(
        id="tr-in", slug="inputs", name="i", config=DelayedTriggerConfig(fire_at=_now() + timedelta(hours=1)), created_at=_now(),
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-in", trigger_id="tr-in", config=SessionAppendSubConfig(session_id="sess-1"), parallelism="skip", created_at=_now(),
    ))


def _refusal(result) -> dict:
    assert result.is_error, result.output
    body = json.loads(result.output)
    assert body["type"] == "validation-error", body
    return body


# ---------------------------------------------------------------------------
# The slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", BAD_SLUGS)
@pytest.mark.parametrize("surface", ["trigger__create", "crud__create_trigger"])
async def test_a_tool_refuses_a_slug_outside_the_pattern(app, surface, slug):
    tools = app.state.provider_registry._trigger_toolset_provider if surface == "trigger__create" else app.state.crud_toolset
    tool_name = "create" if surface == "trigger__create" else "create_trigger"

    result = await tools.call(
        tool_name=tool_name, arguments={"slug": slug, "name": "N", "config": {"kind": "delayed", "fire_at": _fire_at()}},
    )

    assert "slug" in _refusal(result)["message"]
    assert await _triggers(app.state.storage_provider) == []


@pytest.mark.parametrize("slug", BAD_SLUGS)
async def test_rest_refuses_a_slug_outside_the_pattern_with_422(app, client, slug):
    response = await client.post("/v1/triggers", json={"slug": slug, "name": "N", "config": {"kind": "delayed", "fire_at": _fire_at()}})

    assert response.status_code == 422, response.text
    assert any("slug" in error["loc"] for error in response.json()["extensions"]["errors"])
    assert await _triggers(app.state.storage_provider) == []


async def test_a_slug_inside_the_pattern_is_still_taken(app, client):
    tools = app.state.provider_registry._trigger_toolset_provider
    made = await tools.call(tool_name="create", arguments={"slug": "nightly-2", "name": "N", "config": {"kind": "delayed", "fire_at": _fire_at()}})
    assert not made.is_error, made.output
    response = await client.post("/v1/triggers", json={"slug": "ab", "name": "N", "config": {"kind": "delayed", "fire_at": _fire_at()}})
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# parallelism
# ---------------------------------------------------------------------------


async def test_a_tool_create_refuses_a_parallelism_that_is_not_skip_or_queue(app):
    await _seed_trigger_with_sub(app.state.storage_provider)
    tools = app.state.provider_registry._trigger_toolset_provider

    result = await tools.call(tool_name="create_subscription", arguments={
        "trigger_id": "tr-in", "config": {"kind": "session_append", "session_id": "sess-1"}, "parallelism": "parallel",
    })

    assert "parallelism" in _refusal(result)["message"]
    subs = (await app.state.storage_provider.get_storage(Subscription).list(OffsetPage(offset=0, length=50))).items
    assert [s.id for s in subs] == ["sb-in"], "nothing is stored"


async def test_a_tool_update_refuses_a_parallelism_that_is_not_skip_or_queue_and_stores_nothing(app):
    sp = app.state.storage_provider
    await _seed_trigger_with_sub(sp)
    tools = app.state.provider_registry._trigger_toolset_provider

    result = await tools.call(tool_name="update_subscription", arguments={
        "trigger_id": "tr-in", "subscription_id": "sb-in", "parallelism": "parallel",
    })

    assert "parallelism" in _refusal(result)["message"]
    assert (await sp.get_storage(Subscription).get("sb-in")).parallelism == "skip"


async def test_create_channel_binding_refuses_a_parallelism_that_is_not_skip_or_queue(app):
    await _seed_trigger_with_sub(app.state.storage_provider)

    result = await app.state.system_toolset.call(tool_name="create_channel_binding", arguments={
        "trigger_id": "tr-in", "config": {"kind": "session_append", "session_id": "sess-1"}, "parallelism": "parallel",
    })

    assert "parallelism" in _refusal(result)["message"]


async def test_rest_refuses_a_parallelism_that_is_not_skip_or_queue_on_create_and_update(app, client):
    sp = app.state.storage_provider
    await _seed_trigger_with_sub(sp)

    created = await client.post("/v1/triggers/tr-in/subscriptions", json={
        "config": {"kind": "session_append", "session_id": "sess-1"}, "parallelism": "parallel",
    })
    updated = await client.put("/v1/triggers/tr-in/subscriptions/sb-in", json={"parallelism": "parallel"})

    for response in (created, updated):
        assert response.status_code == 422, response.text
        assert any("parallelism" in error["loc"] for error in response.json()["extensions"]["errors"])
    assert (await sp.get_storage(Subscription).get("sb-in")).parallelism == "skip"


@pytest.mark.parametrize("value", ["skip", "queue"])
async def test_skip_and_queue_are_still_taken_on_update(app, client, value):
    sp = app.state.storage_provider
    await _seed_trigger_with_sub(sp)
    tools = app.state.provider_registry._trigger_toolset_provider

    result = await tools.call(tool_name="update_subscription", arguments={"trigger_id": "tr-in", "subscription_id": "sb-in", "parallelism": value})
    assert not result.is_error, result.output
    assert (await sp.get_storage(Subscription).get("sb-in")).parallelism == value
    response = await client.put("/v1/triggers/tr-in/subscriptions/sb-in", json={"parallelism": value})
    assert response.status_code == 200, response.text
