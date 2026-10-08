"""``GET /v1/sessions/{id}`` folds the log for its usage only when the log changed (architecture review A-05).

Every detail read used to read the whole ``messages.jsonl`` (across the sandbox boundary on a docker or k8s workspace), parse it and replay-fold it
(``session_usage``), however long the session and however often the console polls. The fold is a pure function of the log, so it is kept per
LOG IDENTITY: the file's size and modification time as the workspace reports them, taken BEFORE the read (a log that grows between the stat and the
read is served under the older identity and refolded on the next request, never the reverse), plus the row's ``last_seq`` and ``turn_no``. A workspace
that cannot stat the file is never cached. These tests count the reads a fake workspace sees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from primer.api.routers import sessions as sessions_router
from primer.model.except_ import NotFoundError
from primer.model.workspace import FileEntry
from primer.model.workspace_session import WorkspaceSession
from primer.session.usage_cache import UsageCache
from tests.api.test_session_usage_and_context_length import (
    _DONE_LINES,
    _seed_agent_and_profile,
    _seed_session,
)

LOG = ".state/sessions/s-1/messages.jsonl"
SECOND_TURN = (
    '{"seq":3,"kind":"user_input","payload":{"text":"again"}}\n'
    '{"seq":4,"kind":"done","payload":{"usage":{"input_tokens":200,"output_tokens":50}}}\n'
)


class _Workspace:
    """Serves one log; counts reads and stats; the modification time moves with every write."""

    state_path = ".state"

    def __init__(self, *, can_stat: bool = True) -> None:
        self.content = _DONE_LINES.encode()
        self.modified = datetime(2026, 10, 8, tzinfo=UTC)
        self.reads = 0
        self.stats = 0
        self._can_stat = can_stat

    def append(self, text: str) -> None:
        self.content += text.encode()
        self.modified += timedelta(seconds=1)

    async def read_file(self, path: str) -> bytes:
        if path != LOG:
            raise NotFoundError(path)
        self.reads += 1
        return self.content

    async def file_info(self, path: str) -> FileEntry:
        self.stats += 1
        if not self._can_stat:
            raise RuntimeError("this backend cannot stat")
        return FileEntry(path=path, kind="file", size_bytes=len(self.content), modified_at=self.modified)


@pytest.fixture(autouse=True)
def _fresh_cache():
    sessions_router.USAGE_CACHE.clear()
    yield
    sessions_router.USAGE_CACHE.clear()


async def _serve(client: httpx.AsyncClient, app, fake_storage_provider, workspace: _Workspace) -> None:
    await _seed_session(fake_storage_provider, "s-1")
    await _seed_agent_and_profile(fake_storage_provider)

    async def _get(wid):
        return workspace if wid == "ws-1" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]


async def _usage(client: httpx.AsyncClient) -> dict:
    r = await client.get("/v1/sessions/s-1")
    assert r.status_code == 200, r.text
    return r.json()["usage"]


@pytest.mark.asyncio
async def test_an_unchanged_log_is_read_and_folded_once_across_detail_reads(client, app, fake_storage_provider):
    workspace = _Workspace()
    await _serve(client, app, fake_storage_provider, workspace)

    first, second, third = await _usage(client), await _usage(client), await _usage(client)

    assert first == second == third and first["total_input_tokens"] == 1000
    assert workspace.reads == 1, f"the log was read {workspace.reads} times for three detail reads of an unchanged session"


@pytest.mark.asyncio
async def test_an_appended_record_is_in_the_next_read(client, app, fake_storage_provider):
    workspace = _Workspace()
    await _serve(client, app, fake_storage_provider, workspace)
    assert (await _usage(client))["total_input_tokens"] == 1000

    workspace.append(SECOND_TURN)
    after = await _usage(client)

    assert after["total_input_tokens"] == 1200 and after["turns"] == 2
    assert workspace.reads == 2


@pytest.mark.asyncio
async def test_a_log_that_changed_without_changing_size_or_time_is_still_refolded_when_the_row_moved(client, app, fake_storage_provider):
    """A rewrite that keeps the size and the second (a rewind marker the backend stamps with a coarse clock) still moves the row's last_seq."""
    workspace = _Workspace()
    await _serve(client, app, fake_storage_provider, workspace)
    await _usage(client)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get("s-1")
    await sessions.update(row.model_copy(update={"last_seq": row.last_seq + 1}))

    await _usage(client)

    assert workspace.reads == 2


@pytest.mark.asyncio
async def test_a_workspace_that_cannot_stat_the_log_is_never_cached(client, app, fake_storage_provider):
    workspace = _Workspace(can_stat=False)
    await _serve(client, app, fake_storage_provider, workspace)

    first, second = await _usage(client), await _usage(client)

    assert first == second and first["total_input_tokens"] == 1000
    assert workspace.reads == 2


def test_the_cache_is_bounded_and_drops_the_least_recently_used():
    cache = UsageCache(max_entries=2)
    cache.put("a", {"turns": 1})
    cache.put("b", {"turns": 2})
    assert cache.get("a") == {"turns": 1}      # touching "a" makes "b" the oldest
    cache.put("c", {"turns": 3})

    assert cache.get("b") is None
    assert cache.get("a") == {"turns": 1} and cache.get("c") == {"turns": 3}


def test_a_cached_value_cannot_be_changed_through_what_get_returns():
    cache = UsageCache()
    cache.put("a", {"turns": 1})
    cache.get("a")["turns"] = 99                # type: ignore[index]

    assert cache.get("a") == {"turns": 1}
