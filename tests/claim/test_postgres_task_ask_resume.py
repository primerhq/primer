"""Postgres parity for the dormant task-ask staged CAS protocol.

The reference and SQLite contract lives in ``test_task_ask_resume``. This
module runs only in the dedicated Postgres lane and proves the same scalar
guards and cross-row stages execute against JSONB storage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from primer.claim.task_ask_resume import (
    TaskAskResumeOutcome,
    project_task_ask_responder,
    promote_task_ask_resumable,
)
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import WorkspaceSession
from primer.storage.postgres import PostgresStorageProvider
from tests.claim.test_task_ask_resume import (
    TASK_ID,
    _Engine,
    _session,
    _stage,
    _task,
)
from tests.pg_gate import explicit_port, needs_postgres, require_postgres_url


_needs_pg = needs_postgres("task ask staged CAS tests")


def _config(url: str) -> PostgresConfig:
    parsed = urlparse(url)
    return PostgresConfig(
        hostname=parsed.hostname or "localhost",
        port=explicit_port(parsed),
        username=parsed.username or "postgres",
        password=parsed.password or "",  # type: ignore[arg-type]
        database=(parsed.path or "/postgres").lstrip("/") or "postgres",
        db_schema=parse_qs(parsed.query).get("schema", ["public"])[0],
        pool=PoolConfig(min_size=1, max_size=2),
    )


@pytest_asyncio.fixture
async def pg_storage() -> AsyncIterator[PostgresStorageProvider]:
    provider = PostgresStorageProvider(_config(
        require_postgres_url("task ask staged CAS tests"),
    ))
    await provider.initialize()
    tasks = provider.get_storage(ToolCallTask)
    sessions = provider.get_storage(WorkspaceSession)
    for storage, row_id in ((tasks, TASK_ID), (sessions, "sess-1")):
        if await storage.get(row_id) is not None:
            await storage.delete(row_id)
    try:
        yield provider
    finally:
        for storage, row_id in ((tasks, TASK_ID), (sessions, "sess-1")):
            if await storage.get(row_id) is not None:
                await storage.delete(row_id)
        await provider.aclose()


@_needs_pg
@pytest.mark.asyncio
async def test_postgres_stages_projects_promotes_and_repairs_lease(pg_storage) -> None:
    tasks = pg_storage.get_storage(ToolCallTask)
    sessions = pg_storage.get_storage(WorkspaceSession)
    await tasks.create(_task())
    await sessions.create(_session())
    engine = _Engine(fail_once=True)

    assert await _stage(tasks, engine) is TaskAskResumeOutcome.RECEIPT_STAGED
    assert await project_task_ask_responder(
        TASK_ID, task_storage=tasks, session_storage=sessions, engine=engine,
    ) is TaskAskResumeOutcome.PROJECTED
    with pytest.raises(RuntimeError, match="lease write failed"):
        await promote_task_ask_resumable(
            TASK_ID, task_storage=tasks, engine=engine,
        )
    assert (await tasks.get(TASK_ID)).state is ToolCallTaskState.QUEUED
    assert await promote_task_ask_resumable(
        TASK_ID, task_storage=tasks, engine=engine,
    ) is TaskAskResumeOutcome.PROMOTED
    assert engine.calls == 2
