"""The destroy's ``terminating`` mark carries the time it was written, so the probe can tell a stale one (review W52).

A process that crashed in the middle of a destroy left the row ``terminating``, which nothing ever moved again (the probe
skips such a row). The probe now fails a mark older than its staleness bound
(``tests/workspace/test_probe_fails_a_stale_terminating_mark.py``); this pins the half that writes the time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.model.workspace import Workspace, WorkspaceProvider, WorkspaceTemplate
from tests.api.test_workspaces import _provider, _template, sp, wsr  # noqa: F401  (fixtures)


@pytest.mark.asyncio
async def test_the_mark_is_written_with_its_time(sp, wsr) -> None:  # noqa: F811
    await sp.get_storage(WorkspaceProvider).create(_provider())
    await sp.get_storage(WorkspaceTemplate).create(_template())
    row = await wsr.create(template=_template(), workspace_id="ws-mark")
    backend = await wsr.get_backend("local-1")
    real_destroy = backend.destroy
    seen: list[Workspace] = []

    async def _destroy(workspace_id):
        seen.append(await sp.get_storage(Workspace).get(workspace_id))
        await real_destroy(workspace_id)

    backend.destroy = _destroy
    before = datetime.now(timezone.utc)

    await wsr.destroy(row.id)

    (mid,) = seen
    assert mid.phase == "terminating"
    assert mid.terminating_since is not None
    assert before - timedelta(seconds=1) <= mid.terminating_since <= datetime.now(timezone.utc)
