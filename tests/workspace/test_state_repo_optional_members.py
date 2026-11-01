"""CV-12: the optional StateRepo members the callers reach through ``getattr`` are declared and checked.

``stat_state_file`` (the message-log cache), ``supports_append`` and ``commit(appends=)`` (append-only ``messages.jsonl`` writes) are only on the
local repo; the sandbox repo lacks them and the callers degrade. Declared in :mod:`primer.int.state_repo` as optional protocols, so a rename or a
changed return type in the local repo fails here.
"""

from __future__ import annotations

import inspect
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.int.state_repo import AppendingStateRepo, StateRepo, StatingStateRepo
from primer.model.workspace_session import AgentBinding, SessionInfo, SessionStatus

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")


@pytest.fixture
async def repo(tmp_path: Path):
    from primer.workspace.local.state import LocalStateRepo

    r = LocalStateRepo(tmp_path / ".state", workspace_id="ws-opt")
    await r.initialize()
    now = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
    await r.create_session(
        SessionInfo(session_id="s1", agent_id="a1", workspace_id="ws-opt", status=SessionStatus.RUNNING, started_at=now, last_activity_at=now),
        AgentBinding(agent_id="a1", agent_name="A"),
    )
    return r


def test_the_protocols_name_what_the_callers_use():
    assert "stat_state_file" in dir(StatingStateRepo)
    assert "supports_append" in AppendingStateRepo.__annotations__
    assert "appends" in inspect.signature(AppendingStateRepo.commit).parameters


async def test_the_local_repo_has_the_optional_members(repo):
    assert isinstance(repo, StateRepo) and isinstance(repo, StatingStateRepo) and isinstance(repo, AppendingStateRepo)
    assert repo.supports_append is True
    assert "appends" in inspect.signature(repo.commit).parameters
    assert set(inspect.signature(AppendingStateRepo.commit).parameters) - {"self"} <= set(inspect.signature(repo.commit).parameters)


async def test_stat_state_file_answers_size_and_mtime_or_none(repo):
    assert await repo.stat_state_file("sessions/s1/nothing.txt") is None
    await repo.commit("s1", summary="m", op="message", files={"x.txt": b"abc"})
    size, mtime_ns = await repo.stat_state_file("sessions/s1/x.txt")
    assert size == 3 and isinstance(mtime_ns, int)


async def test_commit_appends_extends_a_file_instead_of_replacing_it(repo):
    await repo.commit("s1", summary="one", op="message", appends={"messages.jsonl": b'{"a": 1}\n'})
    await repo.commit("s1", summary="two", op="message", appends={"messages.jsonl": b'{"a": 2}\n'})
    assert await repo.read_state_file("sessions/s1/messages.jsonl") == b'{"a": 1}\n{"a": 2}\n'


async def test_the_sandbox_repo_is_still_a_state_repo_without_them():
    from primer.workspace.sandbox.state import SandboxStateRepo

    assert not hasattr(SandboxStateRepo, "stat_state_file") and not getattr(SandboxStateRepo, "supports_append", False)
    assert "appends" not in inspect.signature(SandboxStateRepo.commit).parameters
