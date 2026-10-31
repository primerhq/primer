"""A subscription stored with a ``parallelism`` that is not ``skip`` or ``queue`` still reads, lists and fires (ticket 01a16ddb, review of #915).

Until the arguments were typed (ruling 5), ``trigger__update_subscription`` and ``PUT .../subscriptions/{sid}`` stored whatever string they
were given (``update_subscription`` assigns the field, which pydantic does not validate), and main holds such rows. The model's ``Literal``
then refused the row on every read: a list, a get or a fire of the trigger failed. A stored row's unknown value now reads as ``skip`` (the
default; read path tolerant, :func:`primer.model.common.reading_a_stored_row`), a request still gets the refusal, and
``service.update_subscription`` refuses such a value itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import primer.trigger.dispatch as dispatch_mod
from primer.model.provider import SqliteConfig
from primer.model.trigger import DelayedTriggerConfig, SessionAppendSubConfig, Subscription, Trigger
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.trigger import build_trigger_toolset_provider
from primer.trigger import service
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps, SubscriptionDispatchResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _mcp_admin():
    """These tools run with no calling session: the MCP endpoint, whose live actor is the person they act as (access-control P2 T6)."""
    from primer.mcp.server import current_actor
    from primer.model.principal import Principal

    token = current_actor.set(Principal(type="user", id="admin-mcp", display="admin-mcp", role="admin", source="local", user_id="admin-mcp"))
    yield
    current_actor.reset(token)


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "stored.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _store_a_row_main_wrote(sp) -> None:
    """A subscription whose update stored ``parallelism: "parallel"``, written the way main's ``update_subscription`` wrote it."""
    await sp.get_storage(Trigger).create(Trigger(
        id="tr-st", slug="stored", name="s", config=DelayedTriggerConfig(fire_at=_now() + timedelta(hours=1)), created_at=_now(),
    ))
    sub = Subscription(id="sb-st", trigger_id="tr-st", config=SessionAppendSubConfig(session_id="sess-1"), created_at=_now())
    await sp.get_storage(Subscription).create(sub)
    sub.parallelism = "parallel"  # an assignment is not validated: what update_subscription did with the value it was given
    await sp.get_storage(Subscription).update(sub)


async def test_the_row_reads_as_skip(sp):
    await _store_a_row_main_wrote(sp)

    row = await sp.get_storage(Subscription).get("sb-st")

    assert row is not None and row.parallelism == "skip"


async def test_the_row_lists_through_the_service_and_the_tools(sp):
    await _store_a_row_main_wrote(sp)
    tools = build_trigger_toolset_provider(storage_provider=sp)

    listed = await service.list_subscriptions(trigger_id="tr-st", deps=service.ServiceDeps(storage_provider=sp))
    by_tool = await tools.call(tool_name="list_subscriptions", arguments={"trigger_id": "tr-st"})
    got = await tools.call(tool_name="get_subscription", arguments={"trigger_id": "tr-st", "subscription_id": "sb-st"})

    assert [s.id for s in listed] == ["sb-st"]
    assert not by_tool.is_error and [s["parallelism"] for s in json.loads(by_tool.output)] == ["skip"]
    assert not got.is_error and json.loads(got.output)["parallelism"] == "skip"


async def test_the_row_fires(sp, monkeypatch):
    await _store_a_row_main_wrote(sp)
    dispatched: list[str] = []

    class _Records:
        async def dispatch(self, sub, **_kw):
            dispatched.append(f"{sub.id}:{sub.parallelism}")
            return SubscriptionDispatchResult(ok=True, artefact_id="sess-1")

    monkeypatch.setattr(dispatch_mod, "get_dispatcher", lambda kind: _Records())

    fired = await fire_trigger(trigger_id="tr-st", scheduled_for=None, deps=DispatchDeps(storage_provider=sp, claim_engine=None))

    assert dispatched == ["sb-st:skip"]
    assert [r["ok"] for r in fired.results] == [True]


def test_a_request_is_still_refused():
    with pytest.raises(ValidationError):
        Subscription.model_validate({
            "id": "sb-x", "trigger_id": "tr-x", "config": {"kind": "session_append", "session_id": "s"}, "parallelism": "parallel",
            "created_at": _now().isoformat(),
        })


async def test_update_subscription_refuses_a_value_that_is_not_skip_or_queue(sp):
    await sp.get_storage(Trigger).create(Trigger(
        id="tr-st", slug="stored", name="s", config=DelayedTriggerConfig(fire_at=_now() + timedelta(hours=1)), created_at=_now(),
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-st", trigger_id="tr-st", config=SessionAppendSubConfig(session_id="sess-1"), parallelism="queue", created_at=_now(),
    ))

    with pytest.raises(ValueError, match="parallelism"):
        await service.update_subscription(
            trigger_id="tr-st", subscription_id="sb-st", parallelism="parallel",  # type: ignore[arg-type]
            deps=service.ServiceDeps(storage_provider=sp),
        )

    assert (await sp.get_storage(Subscription).get("sb-st")).parallelism == "queue"
