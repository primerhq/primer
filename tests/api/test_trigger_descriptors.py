"""The trigger and wait descriptors say what the tools do (ticket 01a16ddb, lead ruling 6).

* ``trigger__fire_now``'s answer is ``{skipped, fire_id, results}``; the descriptor said ``{fire_id, results}``.
* ``trigger__list``'s ``kind`` filter (and ``GET /v1/triggers?kind``) takes any of the four kinds; it said "delayed / scheduled".
* ``system__wait_for_event``'s example waited on ``collection.document_pushed``, a kind a run that is not an admin's is refused (``forbidden``).
* The subscribe tools called with no session answered ``trigger_not_found_or_disabled``, the type of an unknown trigger; they now answer
  ``unsupported-surface``, as ``system__wait_for_event`` does, before the trigger is looked up.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from primer.events.visibility import user_safe_glob
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.trigger import ChannelTriggerConfig, DelayedTriggerConfig, Subscription, Trigger, TriggerKind
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset.events_wait import _WaitForEventArgs, make_wait_for_event_handler
from primer.toolset.trigger import TOOL_FIRE_NOW, TOOL_LIST, TOOL_SUBSCRIBE, TOOL_SUBSCRIBE_CHANNEL
from primer.toolset.workspace_ext import build_workspace_ext_toolset

USER = PrincipalRef(type="user", id="u-1", display="user", role="user", source="local")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _tool(toolset, tool_id: str):
    async for tool in toolset.list_tools():
        if tool.id == tool_id:
            return tool
    raise AssertionError(f"no tool {tool_id!r}")


async def test_fire_now_names_every_key_of_its_answer(app):
    sp = app.state.storage_provider
    await sp.get_storage(Trigger).create(Trigger(
        id="tr-d", slug="described", name="d", config=DelayedTriggerConfig(fire_at=_now() + timedelta(hours=1)), created_at=_now(),
    ))

    result = await app.state.provider_registry._trigger_toolset_provider.call(tool_name="fire_now", arguments={"id": "tr-d"})

    answer = json.loads(result.output)
    assert set(answer) == {"skipped", "fire_id", "results"}
    for key in answer:
        assert key in TOOL_FIRE_NOW.description, f"the fire_now descriptor does not name {key!r}"
    assert all("skipped" in (example.returns or "") for example in TOOL_FIRE_NOW.examples)


async def test_list_names_all_four_kinds_for_its_kind_filter(app):
    described = TOOL_LIST.args_schema["properties"]["kind"]["description"]
    for kind in TriggerKind:
        assert kind.value in TOOL_LIST.description, f"trigger__list's description leaves out {kind.value!r}"
        assert kind.value in described, f"trigger__list's kind argument leaves out {kind.value!r}"
    [param] = [p for p in app.openapi()["paths"]["/v1/triggers"]["get"]["parameters"] if p["name"] == "kind"]
    for kind in TriggerKind:
        assert kind.value in param["description"], f"GET /v1/triggers?kind leaves out {kind.value!r}"


async def test_wait_for_event_shows_a_wait_a_run_that_is_not_an_admins_may_make(app, tmp_path):
    """The example is a real wait for a user-role run: the handler takes it (it parks) instead of answering ``forbidden``."""
    tool = await _tool(app.state.system_toolset, "wait_for_event")
    handler = make_wait_for_event_handler(app.state.storage_provider)
    for example in tool.examples:
        assert all(user_safe_glob(glob) for glob in example.args["event_types"]), example.args
        result = await handler(
            example.args, ctx=ToolContext(tool_call_id="call-1", session_id="sess-3f9a1c8d2b7e", workspace_id="ws-1", initiated_by=USER),
        )
        assert isinstance(result, Yielded), result.output
    described = _WaitForEventArgs.model_json_schema()["properties"]["event_types"]["description"]
    shown = re.findall(r'\["([^"\]]+)"\]', described)
    assert shown, described
    assert all(user_safe_glob(glob) for glob in shown), f"the event_types description shows a kind a non-admin run is refused: {shown}"


@pytest.mark.parametrize(("tool", "trigger"), [
    ("subscribe_to_trigger", Trigger(
        id="tr-s", slug="subscribed", name="s", config=DelayedTriggerConfig(fire_at=datetime(2030, 1, 1, tzinfo=timezone.utc)),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )),
    ("subscribe_to_channel_event", Trigger(
        id="tr-s", slug="subscribed", name="s", config=ChannelTriggerConfig(provider_id="cp-1"),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )),
])
@pytest.mark.parametrize("exists", [True, False], ids=["an enabled trigger", "an unknown trigger"])
async def test_a_subscribe_tool_with_no_session_answers_unsupported_surface(app, tool, trigger, exists):
    sp = app.state.storage_provider
    if exists:
        await sp.get_storage(Trigger).create(trigger)
    toolset = build_workspace_ext_toolset(storage_provider=sp)

    result = await toolset.call(
        tool_name=tool, arguments={"trigger_id": "tr-s"}, ctx=ToolContext(tool_call_id="call-1", session_id=None, workspace_id=None, chat_id="chat-1"),
    )

    assert result.is_error
    assert json.loads(result.output)["type"] == "unsupported-surface", result.output
    assert (await sp.get_storage(Subscription).list(OffsetPage(offset=0, length=10))).items == [], "nothing is parked"


@pytest.mark.parametrize("descriptor", [TOOL_SUBSCRIBE, TOOL_SUBSCRIBE_CHANNEL], ids=lambda d: d.id)
def test_the_subscribe_descriptors_name_the_no_session_answer_and_what_a_wake_returns(descriptor):
    assert "unsupported-surface" in descriptor.description
    for answer in ("timed_out", "cancelled", "fire_context"):
        assert answer in descriptor.description, f"{descriptor.id} does not say it answers {answer!r}"
