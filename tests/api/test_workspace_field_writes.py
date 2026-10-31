"""The routes and tools that change one field of a workspace write that field only (round 2 of #917, item 3).

They read the row and wrote it back whole, so a write whose read came before another writer moved the ``phase`` put the old phase back. The
reviewer's probe: a rename that reads the row while a delete has it ``terminating`` and writes after the failed delete marked it ``failed`` left
the row ``terminating`` for ever (the probe never looks at such a row again). The same holds for a read before the delete (``running`` put back
over ``failed``, its ``failure_reason`` cleared), for every one of the seven writers: the REST rename, events, terminal access, reply binding set
and clear, and the ``system__set_reply_binding`` / ``system__clear_reply_binding`` tools.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest

from primer.model.access import Access
from primer.model.channel import Channel
from primer.model.except_ import ProviderError
from primer.model.workspace import Workspace, WorkspaceProvider, WorkspaceTemplate
from primer.toolset.system import build_system_toolset
from tests.api.test_workspaces import _provider, _template, app, client, pr, sp, wsr  # noqa: F401  (fixtures)

WID = "ws-race"


@pytest.fixture
async def running(sp, wsr) -> Workspace:  # noqa: F811
    """A workspace made through the one create path and promoted as the probe would; a channel to bind it to."""
    await sp.get_storage(WorkspaceProvider).create(_provider())
    await sp.get_storage(WorkspaceTemplate).create(_template())
    await sp.get_storage(Channel).create(Channel(access=Access(public=True), id="ch-1", provider_id="cp-1", provider="slack", external_id="C1"))
    row = await wsr.create(template=_template(), workspace_id=WID)
    return await sp.get_storage(Workspace).patch_if(row.id, {"phase": "running"}, where={"phase": ["pending"]})


async def _fail_a_delete(sp, wsr) -> Workspace:  # noqa: F811
    """Delete the workspace with a teardown that fails; return the row as read in the middle of it (``terminating``)."""
    backend = await wsr.get_backend("local-1")
    seen: dict[str, Workspace] = {}

    async def _broken(workspace_id):
        seen["row"] = await sp.get_storage(Workspace).get(workspace_id)
        raise ProviderError("the volume is busy")

    backend.destroy = _broken
    with pytest.raises(ProviderError):
        await wsr.destroy(WID)
    assert seen["row"].phase == "terminating"
    return seen["row"]


def _serve_once(sp, stale: Workspace) -> None:  # noqa: F811
    """The writer's own read of the row returns ``stale``: the read that came before the phase moved."""
    store = sp.get_storage(Workspace)
    real_get = store.get
    served = {"done": False}

    async def _get(id, *args, **kwargs):  # noqa: A002
        if id == WID and not served["done"]:
            served["done"] = True
            return stale
        return await real_get(id, *args, **kwargs)

    store.get = _get


def _system(sp, pr):  # noqa: F811
    return build_system_toolset(storage_provider=sp, provider_registry=pr)


Writer = Callable[..., Awaitable[None]]


async def _rename(client, sp, pr) -> None:  # noqa: F811
    assert (await client.patch(f"/v1/workspaces/{WID}", json={"name": "renamed"})).status_code == 200


async def _events(client, sp, pr) -> None:  # noqa: F811
    assert (await client.put(f"/v1/workspaces/{WID}/events", json={"config": {"enabled": True}})).status_code == 200


async def _terminal(client, sp, pr) -> None:  # noqa: F811
    resp = await client.put(f"/v1/workspaces/{WID}/terminal_access", json={"enabled": True})
    assert resp.status_code == 200, resp.text


async def _bind(client, sp, pr) -> None:  # noqa: F811
    resp = await client.put(f"/v1/workspaces/{WID}/reply_binding", json={"channel_id": "ch-1"})
    assert resp.status_code == 200, resp.text


async def _unbind(client, sp, pr) -> None:  # noqa: F811
    assert (await client.delete(f"/v1/workspaces/{WID}/reply_binding")).status_code == 204


async def _bind_tool(client, sp, pr) -> None:  # noqa: F811
    result = await _system(sp, pr).call(tool_name="set_reply_binding", arguments={"workspace_id": WID, "channel_id": "ch-1"})
    assert not result.is_error, result.output


async def _unbind_tool(client, sp, pr) -> None:  # noqa: F811
    result = await _system(sp, pr).call(tool_name="clear_reply_binding", arguments={"workspace_id": WID})
    assert not result.is_error, result.output


def _written(row: Workspace) -> dict:
    return {
        "name": row.name, "events": row.events is not None, "terminal_user_access": row.terminal_user_access,
        "reply_binding": row.reply_binding.channel_id if row.reply_binding else None,
    }


WRITERS = {
    "rename": (_rename, {"name": "renamed"}),
    "events": (_events, {"events": True}),
    "terminal_access": (_terminal, {"terminal_user_access": True}),
    "set_reply_binding": (_bind, {"reply_binding": "ch-1"}),
    "clear_reply_binding": (_unbind, {"reply_binding": None}),
    "system__set_reply_binding": (_bind_tool, {"reply_binding": "ch-1"}),
    "system__clear_reply_binding": (_unbind_tool, {"reply_binding": None}),
}


@pytest.mark.asyncio
async def test_a_rename_read_during_a_failed_delete_does_not_leave_it_terminating(client, sp, pr, wsr, running) -> None:  # noqa: F811
    """The reviewer's probe: read mid-teardown (terminating), written after the failed delete (failed)."""
    mid_teardown = await _fail_a_delete(sp, wsr)
    _serve_once(sp, mid_teardown)

    await _rename(client, sp, pr)

    row = await sp.get_storage(Workspace).get(WID)
    assert (row.name, row.phase) == ("renamed", "failed"), "a row left terminating is never probed again"
    assert "the volume is busy" in row.failure_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", list(WRITERS))
async def test_a_field_write_never_puts_back_the_phase_it_read(client, sp, pr, wsr, running, writer) -> None:  # noqa: F811
    """Read before the delete (running), written after the failed delete (failed): only the writer's own field changes."""
    write, expected = WRITERS[writer]
    if writer.startswith("clear") or writer == "system__clear_reply_binding":
        await _bind(client, sp, pr)
    before = await sp.get_storage(Workspace).get(WID)
    await _fail_a_delete(sp, wsr)
    _serve_once(sp, before)

    await write(client, sp, pr)

    row = await sp.get_storage(Workspace).get(WID)
    assert row.phase == "failed" and "the volume is busy" in row.failure_reason, (writer, row.phase, row.failure_reason)
    assert {k: v for k, v in _written(row).items() if k in expected} == expected
