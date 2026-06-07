"""Tests for the ``subscribe_to_trigger`` yielding tool — Phase 8.2.

Covers:

* Unknown trigger id surfaces ``trigger_not_found_or_disabled`` (no
  Subscription row written).
* A disabled trigger surfaces the same error code.
* A valid call yields via :class:`YieldToWorker` with the expected
  ``event_key`` and ``resume_metadata`` shape AND persists a
  ``parked_session`` Subscription bound to the caller's
  (session_id, tool_call_id).
* Calling from a chat-only context (no session id on the
  :class:`ToolContext`) is rejected with the spec error code.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from primer.model.storage import OffsetPage
from primer.model.trigger import Subscription, Trigger
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.trigger import build_trigger_toolset_provider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _future_iso(seconds: int = 3600) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat()


def _delayed_create_args(slug: str, name: str) -> dict:
    return {
        "slug": slug,
        "name": name,
        "config": {"kind": "delayed", "fire_at": _future_iso()},
        "enabled": True,
    }


@pytest.fixture
def toolset(fake_storage_provider):
    return build_trigger_toolset_provider(
        storage_provider=fake_storage_provider,
        claim_engine=None,
        event_bus=None,
    )


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(
        tool_call_id="tc-st-1",
        session_id="wss-test-1",
        workspace_id="ws-test-1",
    )


# ---------------------------------------------------------------------------
# Unknown / disabled trigger
# ---------------------------------------------------------------------------


class TestUnknownTrigger:
    @pytest.mark.asyncio
    async def test_missing_trigger_returns_tool_error(
        self, toolset, ctx, fake_storage_provider,
    ):
        result = await toolset.call(
            tool_name="subscribe_to_trigger",
            arguments={"trigger_id": "tr-ghost"},
            ctx=ctx,
        )
        assert result.is_error
        body = json.loads(result.output)
        assert body["type"] == "trigger_not_found_or_disabled"

        # No Subscription row was written.
        subs_storage = fake_storage_provider.get_storage(Subscription)
        page = await subs_storage.list(OffsetPage(offset=0, length=10))
        assert list(page.items) == []

    @pytest.mark.asyncio
    async def test_disabled_trigger_rejected(
        self, toolset, ctx, fake_storage_provider,
    ):
        # Create then disable.
        created = await toolset.call(
            tool_name="create",
            arguments=_delayed_create_args("dis-trg", "Disabled"),
        )
        trigger_id = json.loads(created.output)["id"]
        await toolset.call(
            tool_name="update",
            arguments={"id": trigger_id, "enabled": False},
        )

        result = await toolset.call(
            tool_name="subscribe_to_trigger",
            arguments={"trigger_id": trigger_id},
            ctx=ctx,
        )
        assert result.is_error
        body = json.loads(result.output)
        assert body["type"] == "trigger_not_found_or_disabled"

        # Still no Subscription row.
        subs_storage = fake_storage_provider.get_storage(Subscription)
        page = await subs_storage.list(OffsetPage(offset=0, length=10))
        assert list(page.items) == []


# ---------------------------------------------------------------------------
# Happy path — yields + persists Subscription
# ---------------------------------------------------------------------------


class TestSubscribeYields:
    @pytest.mark.asyncio
    async def test_subscribe_yields_and_persists_parked_session_sub(
        self, toolset, ctx, fake_storage_provider,
    ):
        created = await toolset.call(
            tool_name="create",
            arguments=_delayed_create_args("sub-yld", "Yield Host"),
        )
        trigger_id = json.loads(created.output)["id"]

        with pytest.raises(YieldToWorker) as info:
            await toolset.call(
                tool_name="subscribe_to_trigger",
                arguments={"trigger_id": trigger_id},
                ctx=ctx,
            )

        # Yielded sentinel was stamped with the registered tool name
        # and routed off the trigger-specific event key.
        yielded = info.value.yielded
        assert yielded.tool_name == "subscribe_to_trigger"
        assert yielded.event_key == f"trigger:{trigger_id}"
        assert info.value.tool_call_id == ctx.tool_call_id

        # resume_metadata carries the subscription id + trigger id so
        # the resume path can find both without rehydrating history.
        assert yielded.resume_metadata["trigger_id"] == trigger_id
        sub_id = yielded.resume_metadata["subscription_id"]
        assert sub_id.startswith("sb-")

        # A matching parked_session Subscription was persisted.
        subs_storage = fake_storage_provider.get_storage(Subscription)
        sub = await subs_storage.get(sub_id)
        assert sub is not None
        assert sub.trigger_id == trigger_id
        assert sub.config.kind == "parked_session"
        assert sub.config.session_id == ctx.session_id
        assert sub.config.tool_call_id == ctx.tool_call_id
        assert sub.enabled is True
        assert sub.parallelism == "skip"
        assert sub.payload_template is None


# ---------------------------------------------------------------------------
# Chat-only invocation (no session_id) is refused
# ---------------------------------------------------------------------------


class TestChatOnlyRejected:
    @pytest.mark.asyncio
    async def test_no_session_id_returns_tool_error(
        self, toolset, fake_storage_provider,
    ):
        created = await toolset.call(
            tool_name="create",
            arguments=_delayed_create_args("chat-only", "Chat Only"),
        )
        trigger_id = json.loads(created.output)["id"]

        chat_ctx = ToolContext(
            tool_call_id="tc-chat",
            session_id=None,  # chat-only invocation
            workspace_id=None,
        )
        result = await toolset.call(
            tool_name="subscribe_to_trigger",
            arguments={"trigger_id": trigger_id},
            ctx=chat_ctx,
        )
        assert result.is_error
        body = json.loads(result.output)
        assert body["type"] == "trigger_not_found_or_disabled"

        subs_storage = fake_storage_provider.get_storage(Subscription)
        page = await subs_storage.list(OffsetPage(offset=0, length=10))
        assert list(page.items) == []
