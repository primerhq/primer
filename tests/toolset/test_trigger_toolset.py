"""Unit tests for the ``trigger`` internal toolset — Phase 8.1.

Covers the management-tool surface mirroring the REST router:

* ``trigger__list`` returns an empty array when no triggers exist.
* ``trigger__create`` + ``trigger__get`` round-trip a row.
* ``trigger__update`` patches a single field.
* ``trigger__delete`` removes the row.
* ``trigger__create_subscription`` persists a non-parked_session sub.
* ``trigger__fire_now`` returns a body with ``fire_id`` + ``results``.
* parked_session subscriptions are rejected from the public create
  path with the spec error code ``parked_session_only_from_yield``.
* The tool catalogue matches the spec's tool list.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from primer.toolset.trigger import (
    TRIGGER_TOOLSET_ID,
    build_trigger_toolset_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future_iso(seconds: int = 3600) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat()


def _delayed_args(slug: str = "tools-delayed", name: str = "Delayed") -> dict:
    return {
        "slug": slug,
        "name": name,
        "config": {"kind": "delayed", "fire_at": _future_iso()},
        "enabled": True,
    }


def _result_body(result) -> dict:
    return json.loads(result.output)


@pytest.fixture
def toolset(fake_storage_provider):
    return build_trigger_toolset_provider(
        storage_provider=fake_storage_provider,
        claim_engine=None,
        event_bus=None,
    )


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


class TestCatalogue:
    @pytest.mark.asyncio
    async def test_tool_ids_match_spec(self, toolset):
        names = {t.id async for t in toolset.list_tools()}
        expected = {
            "trigger__list",
            "trigger__get",
            "trigger__create",
            "trigger__update",
            "trigger__delete",
            "trigger__fire_now",
            "trigger__list_subscriptions",
            "trigger__get_subscription",
            "trigger__create_subscription",
            "trigger__update_subscription",
            "trigger__delete_subscription",
            "subscribe_to_trigger",
        }
        assert names == expected

    @pytest.mark.asyncio
    async def test_all_tools_carry_toolset_id(self, toolset):
        async for tool in toolset.list_tools():
            assert tool.toolset_id == TRIGGER_TOOLSET_ID


# ---------------------------------------------------------------------------
# list — empty
# ---------------------------------------------------------------------------


class TestList:
    @pytest.mark.asyncio
    async def test_list_empty(self, toolset):
        result = await toolset.call(tool_name="trigger__list", arguments={})
        assert not result.is_error, result.output
        items = json.loads(result.output)
        assert items == []


# ---------------------------------------------------------------------------
# create + get
# ---------------------------------------------------------------------------


class TestCreateAndGet:
    @pytest.mark.asyncio
    async def test_create_then_get(self, toolset, fake_storage_provider):
        created = await toolset.call(
            tool_name="trigger__create",
            arguments=_delayed_args(slug="cg-one", name="CG One"),
        )
        assert not created.is_error, created.output
        body = _result_body(created)
        assert body["slug"] == "cg-one"
        assert body["name"] == "CG One"
        trigger_id = body["id"]
        assert trigger_id.startswith("tr-")
        # next_fire_at is set for an enabled delayed trigger.
        assert body["next_fire_at"] is not None

        # Verify get round-trips the same row.
        got = await toolset.call(
            tool_name="trigger__get",
            arguments={"id": trigger_id},
        )
        assert not got.is_error, got.output
        got_body = _result_body(got)
        assert got_body["id"] == trigger_id

    @pytest.mark.asyncio
    async def test_get_missing_returns_trigger_not_found(self, toolset):
        result = await toolset.call(
            tool_name="trigger__get",
            arguments={"id": "tr-ghost"},
        )
        assert result.is_error
        body = _result_body(result)
        assert body["type"] == "trigger_not_found"

    @pytest.mark.asyncio
    async def test_duplicate_slug_rejected(self, toolset):
        first = await toolset.call(
            tool_name="trigger__create",
            arguments=_delayed_args(slug="dup-slug", name="First"),
        )
        assert not first.is_error, first.output
        second = await toolset.call(
            tool_name="trigger__create",
            arguments=_delayed_args(slug="dup-slug", name="Second"),
        )
        assert second.is_error
        body = _result_body(second)
        assert body["type"] == "trigger_slug_conflict"


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_name(self, toolset):
        created = await toolset.call(
            tool_name="trigger__create",
            arguments=_delayed_args(slug="up-slug", name="Original"),
        )
        body = _result_body(created)
        trigger_id = body["id"]

        updated = await toolset.call(
            tool_name="trigger__update",
            arguments={"id": trigger_id, "name": "Renamed"},
        )
        assert not updated.is_error, updated.output
        ubody = _result_body(updated)
        assert ubody["name"] == "Renamed"
        # The discriminator stayed the same.
        assert ubody["config"]["kind"] == "delayed"

    @pytest.mark.asyncio
    async def test_update_missing(self, toolset):
        result = await toolset.call(
            tool_name="trigger__update",
            arguments={"id": "tr-ghost", "name": "X"},
        )
        assert result.is_error
        body = _result_body(result)
        assert body["type"] == "trigger_not_found"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_row(self, toolset, fake_storage_provider):
        created = await toolset.call(
            tool_name="trigger__create",
            arguments=_delayed_args(slug="del-slug", name="Doomed"),
        )
        body = _result_body(created)
        trigger_id = body["id"]

        deleted = await toolset.call(
            tool_name="trigger__delete",
            arguments={"id": trigger_id},
        )
        assert not deleted.is_error, deleted.output
        assert _result_body(deleted) == {"ok": True}

        # Subsequent get returns trigger_not_found.
        got = await toolset.call(
            tool_name="trigger__get",
            arguments={"id": trigger_id},
        )
        assert got.is_error
        assert _result_body(got)["type"] == "trigger_not_found"

    @pytest.mark.asyncio
    async def test_delete_missing(self, toolset):
        result = await toolset.call(
            tool_name="trigger__delete",
            arguments={"id": "tr-ghost"},
        )
        assert result.is_error
        body = _result_body(result)
        assert body["type"] == "trigger_not_found"


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class TestSubscriptions:
    @pytest.mark.asyncio
    async def test_create_subscription_chat_message(
        self, toolset, fake_storage_provider,
    ):
        created = await toolset.call(
            tool_name="trigger__create",
            arguments=_delayed_args(slug="sub-host", name="Sub Host"),
        )
        trigger_id = _result_body(created)["id"]

        sub_result = await toolset.call(
            tool_name="trigger__create_subscription",
            arguments={
                "trigger_id": trigger_id,
                "config": {"kind": "chat_message", "chat_id": "ch-1"},
                "payload_template": "hello",
            },
        )
        assert not sub_result.is_error, sub_result.output
        sub_body = _result_body(sub_result)
        assert sub_body["trigger_id"] == trigger_id
        assert sub_body["config"]["kind"] == "chat_message"
        assert sub_body["config"]["chat_id"] == "ch-1"
        assert sub_body["id"].startswith("sb-")

        # list_subscriptions surfaces the new row.
        listed = await toolset.call(
            tool_name="trigger__list_subscriptions",
            arguments={"trigger_id": trigger_id},
        )
        assert not listed.is_error
        items = json.loads(listed.output)
        assert len(items) == 1
        assert items[0]["id"] == sub_body["id"]

    @pytest.mark.asyncio
    async def test_parked_session_create_rejected(self, toolset):
        created = await toolset.call(
            tool_name="trigger__create",
            arguments=_delayed_args(slug="ps-host", name="PS Host"),
        )
        trigger_id = _result_body(created)["id"]

        bad = await toolset.call(
            tool_name="trigger__create_subscription",
            arguments={
                "trigger_id": trigger_id,
                "config": {
                    "kind": "parked_session",
                    "session_id": "wss-1",
                    "tool_call_id": "tc-1",
                    "parked_at": _future_iso(0),
                },
            },
        )
        assert bad.is_error
        body = _result_body(bad)
        assert body["type"] == "parked_session_only_from_yield"

    @pytest.mark.asyncio
    async def test_subscription_not_found(self, toolset):
        created = await toolset.call(
            tool_name="trigger__create",
            arguments=_delayed_args(slug="snf-host", name="SNF Host"),
        )
        trigger_id = _result_body(created)["id"]

        got = await toolset.call(
            tool_name="trigger__get_subscription",
            arguments={"trigger_id": trigger_id, "subscription_id": "sb-ghost"},
        )
        assert got.is_error
        assert _result_body(got)["type"] == "subscription_not_found"


# ---------------------------------------------------------------------------
# fire_now
# ---------------------------------------------------------------------------


class TestFireNow:
    @pytest.mark.asyncio
    async def test_fire_now_returns_fire_id_and_results(self, toolset):
        # Create a trigger with no subs — fire_now should still report
        # a fire_id and an empty results list.
        created = await toolset.call(
            tool_name="trigger__create",
            arguments=_delayed_args(slug="fire-host", name="Fire Host"),
        )
        trigger_id = _result_body(created)["id"]

        fired = await toolset.call(
            tool_name="trigger__fire_now",
            arguments={"id": trigger_id},
        )
        assert not fired.is_error, fired.output
        body = _result_body(fired)
        assert body["skipped"] is False
        assert body["fire_id"] is not None
        assert isinstance(body["results"], list)

    @pytest.mark.asyncio
    async def test_fire_now_missing(self, toolset):
        result = await toolset.call(
            tool_name="trigger__fire_now",
            arguments={"id": "tr-ghost"},
        )
        assert result.is_error
        assert _result_body(result)["type"] == "trigger_not_found"
