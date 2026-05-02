"""Tests for matrix.workspace.state."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from matrix.model.session import (
    AgentBinding,
    SessionInfo,
    SessionStatus,
)
from matrix.workspace.state import (
    CommitInfo,
    StateRepo,
)


# ===========================================================================
# Helpers / fixtures
# ===========================================================================


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(
    not _git_available(),
    reason="git CLI not available on PATH",
)


def _make_session_info(
    *,
    session_id: str = "sess-1",
    agent_id: str = "agent-foo",
    workspace_id: str = "ws-1",
    status: SessionStatus = SessionStatus.RUNNING,
) -> SessionInfo:
    now = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
    return SessionInfo(
        session_id=session_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        status=status,
        started_at=now,
        last_activity_at=now,
    )


def _make_binding(
    *,
    agent_id: str = "agent-foo",
    name: str = "Agent Foo",
) -> AgentBinding:
    return AgentBinding(agent_id=agent_id, agent_name=name)


def _git_log_subjects(repo_path: Path) -> list[str]:
    """Read commit subjects via git CLI for cross-checking."""
    out = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _git_log_body(repo_path: Path, sha: str) -> str:
    """Return the commit body (subject + trailers)."""
    return subprocess.run(
        ["git", "-C", str(repo_path), "log", "-1", "--format=%B", sha],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


# ===========================================================================
# Construction + initialize()
# ===========================================================================


class TestConstruction:
    def test_rejects_empty_workspace_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="workspace_id"):
            StateRepo(tmp_path / ".state", workspace_id="")

    async def test_initialize_creates_repo_when_missing(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        assert (tmp_path / ".state" / ".git").is_dir()

    async def test_initialize_is_idempotent(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.initialize()  # second call must not error or destroy state
        assert (tmp_path / ".state" / ".git").is_dir()

    async def test_initialize_repopulates_session_cache(self, tmp_path: Path) -> None:
        # First repo: create a session.
        repo1 = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo1.initialize()
        await repo1.create_session(_make_session_info(), _make_binding())

        # Second repo on the same path: should learn about the session
        # without an explicit create_session call.
        repo2 = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo2.initialize()
        # commit() succeeds only if the session is in the cache.
        sha = await repo2.commit(
            "sess-1",
            summary="sess-1: continue",
            op="message",
        )
        assert len(sha) == 40

    def test_path_and_workspace_id_properties(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-x")
        assert repo.path == tmp_path / ".state"
        assert repo.workspace_id == "ws-x"


# ===========================================================================
# create_session()
# ===========================================================================


class TestCreateSession:
    async def test_writes_session_and_agent_json(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        info = _make_session_info()
        binding = _make_binding()
        await repo.create_session(info, binding)

        slot = tmp_path / ".state" / "sessions" / "sess-1"
        assert slot.is_dir()
        assert (slot / "session.json").exists()
        assert (slot / "agent.json").exists()

        loaded_info = await repo.load_session_info("sess-1")
        loaded_binding = await repo.load_agent_binding("sess-1")
        assert loaded_info == info
        assert loaded_binding == binding

    async def test_attach_commit_subject(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        subjects = _git_log_subjects(tmp_path / ".state")
        assert subjects == ["sess-1: attach"]

    async def test_attach_commit_carries_trailers(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        sha = await repo.create_session(_make_session_info(), _make_binding())
        body = _git_log_body(tmp_path / ".state", sha)
        assert "X-Matrix-Workspace: ws-1" in body
        assert "X-Matrix-Session: sess-1" in body
        assert "X-Matrix-Agent: agent-foo" in body
        assert "X-Matrix-Op: attach" in body
        assert "X-Matrix-Tool" not in body
        assert "X-Matrix-Call" not in body

    async def test_returns_full_sha(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        sha = await repo.create_session(_make_session_info(), _make_binding())
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    async def test_rejects_duplicate_session(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        with pytest.raises(ValueError, match="already exists"):
            await repo.create_session(_make_session_info(), _make_binding())

    @pytest.mark.parametrize("bad_id", ["..", ".", "a/b", "a\\b"])
    async def test_rejects_bad_session_id(self, tmp_path: Path, bad_id: str) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        with pytest.raises(ValueError):
            await repo.create_session(
                _make_session_info(session_id=bad_id),
                _make_binding(),
            )


# ===========================================================================
# commit()
# ===========================================================================


class TestCommit:
    async def test_writes_files_and_commits(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())

        sha = await repo.commit(
            "sess-1",
            summary="sess-1: message",
            op="message",
            files={"messages.jsonl": '{"role":"assistant","content":"hi"}\n'},
        )
        assert len(sha) == 40
        slot = tmp_path / ".state" / "sessions" / "sess-1"
        assert (slot / "messages.jsonl").read_text(encoding="utf-8") == (
            '{"role":"assistant","content":"hi"}\n'
        )

    async def test_writes_bytes_file(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        await repo.commit(
            "sess-1",
            summary="binary blob",
            op="memory_write",
            files={"memory/blob.bin": b"\x00\x01\x02\x03"},
        )
        slot = tmp_path / ".state" / "sessions" / "sess-1"
        assert (slot / "memory" / "blob.bin").read_bytes() == b"\x00\x01\x02\x03"

    async def test_includes_tool_and_call_trailers_when_supplied(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        sha = await repo.commit(
            "sess-1",
            summary="sess-1: tool_call",
            op="tool_call",
            tool="exec",
            call_id="call-42",
        )
        body = _git_log_body(tmp_path / ".state", sha)
        assert "X-Matrix-Tool: exec" in body
        assert "X-Matrix-Call: call-42" in body

    async def test_omits_optional_trailers_when_absent(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        sha = await repo.commit(
            "sess-1",
            summary="status_change",
            op="status_change",
        )
        body = _git_log_body(tmp_path / ".state", sha)
        assert "X-Matrix-Tool" not in body
        assert "X-Matrix-Call" not in body

    async def test_delete_files_removes_in_same_commit(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())

        # Add waiting.json.
        await repo.commit(
            "sess-1",
            summary="enter waiting",
            op="status_change",
            files={"waiting.json": '{"kind":"user_input","prompt":"q?","queued_at":"2026-05-02T10:00:00+00:00"}'},
        )
        slot = tmp_path / ".state" / "sessions" / "sess-1"
        assert (slot / "waiting.json").exists()

        # Remove it.
        await repo.commit(
            "sess-1",
            summary="leave waiting",
            op="status_change",
            delete_files=["waiting.json"],
        )
        assert not (slot / "waiting.json").exists()

    async def test_delete_files_is_idempotent_for_missing(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        # Deleting a file that was never created should not raise.
        await repo.commit(
            "sess-1",
            summary="noop",
            op="status_change",
            delete_files=["waiting.json"],
        )

    async def test_allow_empty_commit_when_no_files(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        # Pure status_change with no file changes.
        sha = await repo.commit(
            "sess-1",
            summary="bare status_change",
            op="status_change",
        )
        assert len(sha) == 40

    async def test_rejects_unknown_op(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        with pytest.raises(ValueError, match="unknown op"):
            await repo.commit(
                "sess-1",
                summary="x",
                op="kaboom",  # type: ignore[arg-type]
            )

    async def test_rejects_unknown_session(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        with pytest.raises(LookupError, match="sess-nope"):
            await repo.commit(
                "sess-nope",
                summary="x",
                op="message",
            )

    @pytest.mark.parametrize("bad_path", ["/abs", "..", "a/../b", "x\x00y"])
    async def test_rejects_bad_relative_path(
        self, tmp_path: Path, bad_path: str
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        with pytest.raises(ValueError):
            await repo.commit(
                "sess-1",
                summary="x",
                op="message",
                files={bad_path: "x"},
            )


# ===========================================================================
# Concurrency (commit lock)
# ===========================================================================


class TestConcurrency:
    async def test_parallel_commits_serialise_without_conflict(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        # Create two sessions.
        await repo.create_session(_make_session_info(session_id="sess-a"), _make_binding(agent_id="agent-a"))
        await repo.create_session(_make_session_info(session_id="sess-b"), _make_binding(agent_id="agent-b"))

        # Fire many commits in parallel; they should all succeed and
        # produce distinct SHAs.
        tasks = []
        for i in range(20):
            sid = "sess-a" if i % 2 == 0 else "sess-b"
            tasks.append(
                repo.commit(
                    sid,
                    summary=f"turn {i}",
                    op="message",
                    files={"messages.jsonl": f"line {i}\n"},
                )
            )
        shas = await asyncio.gather(*tasks)
        assert len(set(shas)) == 20  # all distinct
        # All 22 commits exist on the trunk: 2 attach + 20 messages.
        subjects = _git_log_subjects(tmp_path / ".state")
        assert len(subjects) == 22


# ===========================================================================
# history()
# ===========================================================================


class TestHistory:
    async def test_returns_empty_for_empty_repo(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        out = await repo.history()
        assert out == []

    async def test_returns_all_commits_newest_first(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        await repo.commit("sess-1", summary="turn 1", op="message")
        await repo.commit("sess-1", summary="turn 2", op="message")
        out = await repo.history()
        assert [c.subject for c in out] == ["turn 2", "turn 1", "sess-1: attach"]

    async def test_filter_by_session(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(session_id="sess-a"), _make_binding(agent_id="agent-a"))
        await repo.create_session(_make_session_info(session_id="sess-b"), _make_binding(agent_id="agent-b"))
        await repo.commit("sess-a", summary="A1", op="message")
        await repo.commit("sess-b", summary="B1", op="message")
        out = await repo.history(session_id="sess-a")
        subjects = [c.subject for c in out]
        assert "A1" in subjects
        assert "B1" not in subjects

    async def test_filter_by_agent(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(session_id="sess-a"), _make_binding(agent_id="agent-a"))
        await repo.create_session(_make_session_info(session_id="sess-b"), _make_binding(agent_id="agent-b"))
        await repo.commit("sess-a", summary="A1", op="message")
        await repo.commit("sess-b", summary="B1", op="message")
        out = await repo.history(agent_id="agent-b")
        agent_ids = {c.agent_id for c in out}
        assert agent_ids == {"agent-b"}

    async def test_filter_by_session_and_agent_and_match(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(session_id="sess-a"), _make_binding(agent_id="agent-a"))
        await repo.create_session(_make_session_info(session_id="sess-b"), _make_binding(agent_id="agent-b"))
        await repo.commit("sess-a", summary="A1", op="message")
        # Combined filter restricts further.
        out = await repo.history(session_id="sess-a", agent_id="agent-a")
        assert {c.session_id for c in out} == {"sess-a"}
        assert {c.agent_id for c in out} == {"agent-a"}
        # Mismatched combination yields empty.
        out2 = await repo.history(session_id="sess-a", agent_id="agent-b")
        assert out2 == []

    async def test_limit_caps_returned_commits(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        for i in range(5):
            await repo.commit("sess-1", summary=f"turn {i}", op="message")
        out = await repo.history(limit=3)
        assert len(out) == 3

    async def test_rejects_zero_limit(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        with pytest.raises(ValueError, match="limit"):
            await repo.history(limit=0)

    async def test_returned_records_carry_op_tool_call(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        await repo.commit(
            "sess-1",
            summary="exec ls",
            op="tool_call",
            tool="exec",
            call_id="c-1",
        )
        latest = (await repo.history(limit=1))[0]
        assert isinstance(latest, CommitInfo)
        assert latest.op == "tool_call"
        assert latest.tool == "exec"
        assert latest.call_id == "c-1"
        assert latest.workspace_id == "ws-1"
        assert latest.session_id == "sess-1"
        assert latest.agent_id == "agent-foo"


# ===========================================================================
# read_at()
# ===========================================================================


class TestReadAt:
    async def test_reads_file_from_historical_commit(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        sha1 = await repo.commit(
            "sess-1",
            summary="v1",
            op="message",
            files={"messages.jsonl": "v1\n"},
        )
        sha2 = await repo.commit(
            "sess-1",
            summary="v2",
            op="message",
            files={"messages.jsonl": "v2\n"},
        )
        v1 = await repo.read_at(sha1, "sessions/sess-1/messages.jsonl")
        v2 = await repo.read_at(sha2, "sessions/sess-1/messages.jsonl")
        assert v1 == b"v1\n"
        assert v2 == b"v2\n"

    async def test_raises_for_missing_path(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        sha = await repo.create_session(_make_session_info(), _make_binding())
        with pytest.raises(FileNotFoundError):
            await repo.read_at(sha, "sessions/sess-1/does-not-exist")

    async def test_rejects_empty_sha(self, tmp_path: Path) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        with pytest.raises(ValueError, match="sha"):
            await repo.read_at("", "anything")


# ===========================================================================
# load_session_info / load_agent_binding / load_waiting_state
# ===========================================================================


class TestLoaders:
    async def test_session_info_returns_none_when_missing(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        assert await repo.load_session_info("nope") is None

    async def test_agent_binding_returns_none_when_missing(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        assert await repo.load_agent_binding("nope") is None

    async def test_waiting_state_returns_none_when_missing(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        # Just-created session has no waiting.json.
        assert await repo.load_waiting_state("sess-1") is None

    async def test_waiting_state_round_trips_user_input(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        await repo.commit(
            "sess-1",
            summary="enter waiting",
            op="status_change",
            files={
                "waiting.json": (
                    '{"kind":"user_input","prompt":"How long?",'
                    '"queued_at":"2026-05-02T10:00:00+00:00"}'
                )
            },
        )
        ws = await repo.load_waiting_state("sess-1")
        assert ws is not None
        assert ws.kind == "user_input"
        assert ws.prompt == "How long?"

    async def test_waiting_state_round_trips_tool_approval(
        self, tmp_path: Path
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        await repo.create_session(_make_session_info(), _make_binding())
        await repo.commit(
            "sess-1",
            summary="approval pending",
            op="status_change",
            files={
                "waiting.json": (
                    '{"kind":"tool_approval","tool_id":"exec",'
                    '"arguments":{"command":"rm -rf /tmp/scratch"},'
                    '"rationale":"cleaning up","queued_at":"2026-05-02T10:00:00+00:00"}'
                )
            },
        )
        ws = await repo.load_waiting_state("sess-1")
        assert ws is not None
        assert ws.kind == "tool_approval"
        assert ws.tool_id == "exec"
        assert ws.arguments == {"command": "rm -rf /tmp/scratch"}

    @pytest.mark.parametrize(
        "loader_name",
        ["load_session_info", "load_agent_binding", "load_waiting_state"],
    )
    async def test_loaders_validate_session_id(
        self, tmp_path: Path, loader_name: str
    ) -> None:
        repo = StateRepo(tmp_path / ".state", workspace_id="ws-1")
        await repo.initialize()
        loader = getattr(repo, loader_name)
        with pytest.raises(ValueError, match="session_id"):
            await loader("a/b")
