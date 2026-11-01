"""Migration 18: stored channel rows lose the retired ``config.chats.relay_mode`` key and load again (ticket 01a16e37-b98e, item 7).

``relay_mode`` was stored and never read, so it was removed from ``ChatConfig``, which forbids extras: a channel row that still carries it
cannot be read by the live model until the step drops it. Run on real SQLite storage through ``run_migrations``, the way a boot runs it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError

from primer.model.channel import Channel, ChatConfig
from primer.model.except_ import RowDecodeError
from primer.model.provider import SqliteConfig
from primer.storage.migrations import LATEST_VERSION, MIGRATIONS, run_migrations
from primer.storage.migrations.m019_drop_chat_relay_mode import Channel as ChannelView
from primer.storage.migrations.m019_drop_chat_relay_mode import M019DropChatRelayMode
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m019.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _row(row_id: str, provider: str, chats: dict[str, Any]) -> ChannelView:
    """A channel row as an older build stored it, written through the migration's own view (the live model would refuse it)."""
    return ChannelView(
        id=row_id, provider_id=f"cp-{provider}", provider=provider, external_id=f"room-{row_id}", label="ops",
        config={"chats": chats},
    )


async def test_a_stored_row_with_relay_mode_is_unreadable_until_the_step_drops_it_and_then_loads(sp) -> None:
    views = sp.get_storage(ChannelView)
    await views.create(_row("ch-old", "slack", {"enabled": True, "relay_mode": "all"}))
    await views.create(_row("ch-final", "telegram", {"enabled": False, "relay_mode": "final"}))
    await views.create(_row("ch-new", "discord", {"enabled": True}))
    await sp.set_schema_version(18)

    with pytest.raises(RowDecodeError):
        await sp.get_storage(Channel).get("ch-old")

    assert await run_migrations(sp, is_fresh_install=False) == LATEST_VERSION

    old = await sp.get_storage(Channel).get("ch-old")
    assert old.config.chats.model_dump() == {"enabled": True}
    assert old.label == "ops" and old.external_id == "room-ch-old", "the step changed more than the retired key"
    final = await sp.get_storage(Channel).get("ch-final")
    assert final.config.chats.model_dump() == {"enabled": False}
    assert (await sp.get_storage(Channel).get("ch-new")).config.chats.enabled is True
    stored = await views.get("ch-old")
    assert stored.config == {"chats": {"enabled": True}}


async def test_the_step_is_idempotent_and_leaves_a_row_without_the_key_alone(sp) -> None:
    views = sp.get_storage(ChannelView)
    await views.create(_row("ch-old", "slack", {"enabled": True, "relay_mode": "all"}))
    await views.create(_row("ch-new", "slack", {"enabled": False}))
    untouched = await views.get("ch-new")

    step = M019DropChatRelayMode()
    await step.apply(sp)
    await step.apply(sp)

    assert (await views.get("ch-old")).config == {"chats": {"enabled": True}}
    assert await views.get("ch-new") == untouched


async def test_a_row_with_a_null_config_is_read_and_left_alone(sp) -> None:
    """A channel row stored with ``config: null`` reads through the view (round 2 of #932, item 7: the view's config is nullable) and the
    step leaves it alone: there is no chats block to strip, and the live model normalizes a null config to its empty default on read."""
    views = sp.get_storage(ChannelView)
    await views.create(
        ChannelView(id="ch-null", provider_id="cp-sl", provider="slack", external_id="room-ch-null", config=None)
    )
    untouched = await views.get("ch-null")

    await M019DropChatRelayMode().apply(sp)

    assert await views.get("ch-null") == untouched
    loaded = await sp.get_storage(Channel).get("ch-null")
    assert loaded is not None and loaded.config is not None, "the live model answers a null config with its empty default"


def test_relay_mode_is_refused_on_write() -> None:
    """extra="forbid" stays: a request that still sends the key is a validation error, not silently dropped."""
    with pytest.raises(ValidationError, match="relay_mode"):
        ChatConfig.model_validate({"enabled": True, "relay_mode": "final"})


def test_it_is_registered_as_version_19() -> None:
    assert isinstance(MIGRATIONS[18], M019DropChatRelayMode)
    assert MIGRATIONS[18].version == 19
    assert ChannelView.__name__ == Channel.__name__, "the view's class name selects the live channel table"


# -- golden characterization (PERF-T16) ---------------------------------------------------------------------------------------------------

from primer.model.storage import OffsetPage  # noqa: E402
from tests.storage._migration_golden import assert_golden, bulk, insert_raw  # noqa: E402


async def _old_apply(sp) -> None:
    """Migration 19 as it was before PERF-T16, frozen: OFFSET pages into a full list, then ``update`` each row carrying the key."""
    channels = sp.get_storage(ChannelView)
    offset = 0
    rows = []
    while True:
        page = await channels.list(OffsetPage(offset=offset, length=200, want_total=False))
        rows.extend(page.items)
        if len(page.items) < 200:
            break
        offset += 200
    for row in rows:
        chats = (row.config or {}).get("chats")
        if not isinstance(chats, dict) or "relay_mode" not in chats:
            continue
        cleaned = {key: value for key, value in chats.items() if key != "relay_mode"}
        await channels.update(row.model_copy(update={"config": {**row.config, "chats": cleaned}}))


async def _golden_seed(sp) -> None:
    await insert_raw(sp, ChannelView, {
        "c-all": {"label": "ops", "config": {"chats": {"enabled": True, "relay_mode": "all"}, "x": 1.5}},
        "c-null": {"config": {"chats": {"relay_mode": None}}},
        "c-odd": {"config": {"chats": {"relay_mode": {"nested": [1]}, "enabled": False}}},
        "c-none": {"config": {"chats": {"enabled": True}}},
        "c-chats-null": {"config": {"chats": None}},
        "c-chats-list": {"config": {"chats": ["relay_mode"]}},
        "c-chats-str": {"config": {"chats": "relay_mode"}},
        "c-config-null": {"config": None},
        "c-config-absent": {"label": "x"},
        "c-config-empty": {"config": {}},
        **bulk("cb", 450, lambda i: {"config": {"chats": {"relay_mode": "final"} if i % 2 else {"enabled": True}}}),
    })


async def test_golden_rows_match_the_old_implementation_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    await assert_golden(tmp_path, _golden_seed, (ChannelView,), _old_apply, M019DropChatRelayMode().apply)
