"""A destroy marks the workspace ``terminating`` before it tears anything down, and a failed teardown leaves a phase that tells the truth
(lead ruling 3 of ticket 01a16d72).

``terminating`` was in the model (the probe skips it, the reply-binding route refuses it), but nothing wrote it: a delete
ran with the row still ``running`` until the row was gone. :meth:`WorkspaceRegistry.destroy` now writes it once the refusals are past (the default
workspace, an unknown id, the rank scan), before the backend is reached, so a reader sees the delete in progress. If the destroy then fails, the
row must not stay ``terminating`` (nothing is terminating any more, and the probe never looks at such a row again):

* nothing was torn down yet, because the backend could not be resolved (``get_backend`` raised): the phase the mark replaced goes back;
* the teardown itself failed, or the row could not be deleted after it: ``failed``, with a ``failure_reason`` that says the delete did not
  finish, and every open session ended ``workspace_lost``, as on every other transition to ``failed`` (the probe brings an intact workspace back
  to ``running`` after three good probes, and a ``workspace_lost`` session reopens then). An outage during the teardown call itself (a daemon
  that is down, with the backend resolved and cached) is a failed teardown too: the destroy cannot tell what it removed.

The probe wrote its verdict back as the whole row it had listed before the ping, which put back whatever changed meanwhile: the ``terminating``
mark, the ``failed`` a failed destroy leaves, a rename. It now writes only its own fields, fenced on the phase, the reason and the probe time
it judged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from primer.model.except_ import ProviderError
from primer.model.workspace import Workspace, WorkspaceProvider, WorkspaceTemplate
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.toolset.workspaces import build_workspaces_toolset
from tests.api.test_workspaces import _provider, _template, sp, wsr  # noqa: F401  (fixtures)


@pytest.fixture
def toolset(sp, wsr):  # noqa: F811
    return build_workspaces_toolset(storage_provider=sp, workspace_registry=wsr)


@pytest.fixture
async def wid(sp, wsr) -> str:  # noqa: F811
    """A workspace made through the one create path, marked running as the probe would, with one open session."""
    await sp.get_storage(WorkspaceProvider).create(_provider())
    await sp.get_storage(WorkspaceTemplate).create(_template())
    row = await wsr.create(template=_template(), workspace_id="ws-del")
    await sp.get_storage(Workspace).patch_if(row.id, {"phase": "running"}, where={"phase": ["pending"]})
    await sp.get_storage(WorkspaceSession).create(
        WorkspaceSession(
            id="sess-open", workspace_id=row.id, binding=AgentSessionBinding(agent_id="ag-1"), status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
    )
    return row.id


async def _row(sp, workspace_id: str) -> Workspace | None:  # noqa: F811
    return await sp.get_storage(Workspace).get(workspace_id)


async def _session(sp) -> WorkspaceSession:  # noqa: F811
    return await sp.get_storage(WorkspaceSession).get("sess-open")


@pytest.mark.asyncio
async def test_a_reader_sees_terminating_while_the_teardown_runs(sp, wsr, wid, toolset) -> None:  # noqa: F811
    backend = await wsr.get_backend("local-1")
    real_destroy = backend.destroy
    seen: list[str] = []

    async def _destroy(workspace_id):
        # A reader in the middle of the teardown: the stored row, and the get_workspace tool that serves it.
        seen.append((await _row(sp, workspace_id)).phase)
        seen.append(json.loads((await toolset.call(tool_name="get_workspace", arguments={"id": workspace_id})).output)["phase"])
        await real_destroy(workspace_id)

    backend.destroy = _destroy

    await wsr.destroy(wid)

    assert seen == ["terminating", "terminating"]
    assert await _row(sp, wid) is None
    assert (await _session(sp)).ended_reason == "workspace_lost"


@pytest.mark.asyncio
async def test_a_failed_teardown_leaves_the_row_failed_and_says_why(sp, wsr, wid) -> None:  # noqa: F811
    backend = await wsr.get_backend("local-1")

    async def _broken(workspace_id):
        raise ProviderError("the volume could not be removed")

    backend.destroy = _broken

    with pytest.raises(ProviderError):
        await wsr.destroy(wid)

    row = await _row(sp, wid)
    assert row is not None, "a failed teardown keeps the row, so the delete can be asked again"
    assert row.phase == "failed", "a row left terminating would never be probed again, and nothing is terminating any more"
    assert "delete" in row.failure_reason and "the volume could not be removed" in row.failure_reason
    session = await _session(sp)
    assert (session.status, session.ended_reason) == (SessionStatus.ENDED, "workspace_lost"), "a failed workspace has ended its open sessions"
    assert not wsr.is_being_destroyed(wid)


@pytest.mark.asyncio
async def test_a_row_that_cannot_be_deleted_after_the_teardown_is_failed(sp, wsr, wid) -> None:  # noqa: F811
    """The runtime is gone and the sessions are ended, but the row delete failed: the row says so instead of staying terminating."""
    store = sp.get_storage(Workspace)

    async def _no_delete(workspace_id):
        raise ProviderError("database went away")

    store.delete = _no_delete

    with pytest.raises(ProviderError):
        await wsr.destroy(wid)

    row = await _row(sp, wid)
    assert row.phase == "failed"
    assert "database went away" in row.failure_reason
    assert (await _session(sp)).ended_reason == "workspace_lost"


@pytest.mark.asyncio
async def test_a_backend_that_cannot_be_resolved_puts_the_phase_back(sp, wsr, wid, monkeypatch) -> None:  # noqa: F811
    """Nothing was torn down: the workspace is as it was, so its phase is too, and its sessions are untouched."""

    async def _unreachable(provider_id):
        raise ProviderError("the cluster API did not answer")

    monkeypatch.setattr(wsr, "get_backend", _unreachable)

    with pytest.raises(ProviderError):
        await wsr.destroy(wid)

    row = await _row(sp, wid)
    assert (row.phase, row.failure_reason) == ("running", None)
    assert (await _session(sp)).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_the_phase_put_back_is_the_one_read_at_the_mark(sp, wsr, wid, monkeypatch) -> None:  # noqa: F811
    """Round 2 of #917, item 8: the probe failed the workspace while the rank scan ran (after destroy's first read, before the mark), and
    the backend then could not be resolved: the phase put back is the one the mark replaced (``failed``), not the stale ``running``."""

    async def _probe_fails_it_meanwhile():
        await sp.get_storage(Workspace).patch_if(wid, {"phase": "failed", "failure_reason": "runtime unreachable"}, where={"phase": ["running"]})

    async def _unresolvable(provider_id):
        raise ProviderError("the provider's backend could not be built")

    monkeypatch.setattr(wsr, "get_backend", _unresolvable)

    with pytest.raises(ProviderError):
        await wsr.destroy(wid, before_teardown=_probe_fails_it_meanwhile)

    row = await _row(sp, wid)
    assert (row.phase, row.failure_reason) == ("failed", "runtime unreachable")


@pytest.mark.asyncio
async def test_a_refused_rank_scan_never_marks_the_row(sp, wsr, wid) -> None:  # noqa: F811
    phases: list[str] = []

    async def _refuse():
        phases.append((await _row(sp, wid)).phase)
        raise PermissionError("outranked")

    with pytest.raises(PermissionError):
        await wsr.destroy(wid, before_teardown=_refuse)

    assert phases == ["running"], "the rank scan runs before the mark: a refused delete never shows terminating"
    assert (await _row(sp, wid)).phase == "running"
    assert (await _session(sp)).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_the_delete_tool_reports_the_failure_and_the_row_reads_failed(sp, wsr, wid, toolset) -> None:  # noqa: F811
    backend = await wsr.get_backend("local-1")

    async def _broken(workspace_id):
        raise ProviderError("the container would not stop")

    backend.destroy = _broken

    result = await toolset.call(tool_name="delete_workspace", arguments={"id": wid})

    assert result.is_error and json.loads(result.output)["type"] == "backend-error"
    got = json.loads((await toolset.call(tool_name="get_workspace", arguments={"id": wid})).output)
    assert got["phase"] == "failed" and "the container would not stop" in got["failure_reason"]


# ---- the probe does not put back what changed while it pinged ------------------------------------------------------------------------------


async def _tick_while(sp, wsr, wid, change, *, ok: bool) -> None:  # noqa: F811
    """One probe tick of ``wid`` whose ping first makes ``change`` to the row (a write that lands between the probe's read and its write)."""
    from primer.workspace.probe import WorkspaceProbeTask

    async def _ping():
        await sp.get_storage(Workspace).patch_if(wid, change, where={"phase": ["running"]})
        return ok

    (await wsr.get_workspace(wid)).ping = AsyncMock(side_effect=_ping)
    await WorkspaceProbeTask(storage_provider=sp, registry=wsr, interval_seconds=0.01, start_delay_seconds=0).tick()


@pytest.mark.asyncio
async def test_the_probe_does_not_put_back_a_terminating_mark(sp, wsr, wid) -> None:  # noqa: F811
    await _tick_while(sp, wsr, wid, {"phase": "terminating"}, ok=False)

    assert (await _row(sp, wid)).phase == "terminating"


@pytest.mark.asyncio
async def test_the_probe_does_not_put_back_a_failed_destroy_s_phase(sp, wsr, wid) -> None:  # noqa: F811
    await _tick_while(sp, wsr, wid, {"phase": "failed", "failure_reason": "the delete did not finish"}, ok=True)

    row = await _row(sp, wid)
    assert (row.phase, row.failure_reason) == ("failed", "the delete did not finish")


@pytest.mark.asyncio
async def test_the_probe_writes_only_its_own_fields(sp, wsr, wid) -> None:  # noqa: F811
    """A rename that lands while the probe pings survives its write, and the probe's own fields are written."""
    await _tick_while(sp, wsr, wid, {"name": "renamed"}, ok=True)

    row = await _row(sp, wid)
    assert (row.name, row.phase, row.last_probe_ok) == ("renamed", "running", True)
    assert row.last_probe_at is not None


@pytest.mark.asyncio
async def test_a_slower_probe_never_overwrites_a_newer_result(sp, wsr, wid) -> None:  # noqa: F811
    """Round 2 of #917, item 5: the fence also holds ``last_probe_at`` as read, so a probe whose ping outlived a newer probe's write (another
    process, same phase) drops its older verdict instead of writing it over the newer one."""
    newer = "2099-01-01T00:00:00Z"
    await _tick_while(sp, wsr, wid, {"last_probe_at": newer, "last_probe_ok": True}, ok=False)

    row = await _row(sp, wid)
    assert (row.last_probe_ok, row.last_probe_at.year) == (True, 2099)


@pytest.mark.asyncio
async def test_a_failed_destroy_between_two_failed_phases_is_not_undone_by_a_recovery_verdict(sp, wsr, wid) -> None:  # noqa: F811
    """The reviewer's ABA probe: the row is ``failed`` with two good probes counted; during the third ping a delete marks it ``terminating``
    and fails, so it reads ``failed`` again, with the delete's reason. The phase is the one the probe read, but ``failure_reason`` is not: the
    fence holds it too, so the stale "back to running" verdict (a ping made before the teardown) is dropped."""
    from primer.workspace.probe import WorkspaceProbeTask

    store = sp.get_storage(Workspace)
    await store.patch_if(wid, {"phase": "failed", "failure_reason": "runtime unreachable"}, where={"phase": ["running"]})

    async def _ping():
        await store.patch_if(wid, {"phase": "terminating"}, where={"phase": ["failed"]})
        await store.patch_if(wid, {"phase": "failed", "failure_reason": "the delete did not finish (X)"}, where={"phase": ["terminating"]})
        return True

    (await wsr.get_workspace(wid)).ping = AsyncMock(side_effect=_ping)
    probe = WorkspaceProbeTask(storage_provider=sp, registry=wsr, interval_seconds=0.01, start_delay_seconds=0)
    probe._hit_counts[wid] = 2  # noqa: SLF001  (this hit is the third: the verdict would be running)
    await probe.tick()

    row = await _row(sp, wid)
    assert (row.phase, row.failure_reason) == ("failed", "the delete did not finish (X)")
