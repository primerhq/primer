"""Tests for the in-pod git state op handlers (state_commit / state_read / state_history).

These tests call the handlers DIRECTLY with a real git repo in a tmp dir --
no container needed.

Envelope shapes (matches what Task 2.3 RuntimeClient will expect):
  state_commit  -> {"sha": "<40-hex>"}
  state_read    -> {"files": {path: content_b64_or_null}}
  state_history -> {"commits": [<record-dict>, ...]}

Each record dict has keys: sha, subject, committed_at (ISO-8601 str),
workspace_id, session_id, agent_id, op, tool, call_id (all str|None).
"""

from __future__ import annotations

import base64
import subprocess
import tempfile
from pathlib import Path

import pytest

from primer_runtime.ops import state_commit, state_read, state_history


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def fromb64(s: str) -> bytes:
    return base64.b64decode(s)


# ---------------------------------------------------------------------------
# Fixture: tmp workspace_root with a .state git repo
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    """Create a workspace_root with an initialised .state git repo."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", str(state_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(state_dir), "config", "user.email", "primer@local"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(state_dir), "config", "user.name", "primer"], check=True, capture_output=True)
    # Initial empty commit so the repo has a HEAD (needed for git log to work).
    subprocess.run(
        ["git", "-C", str(state_dir), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return tmp_path


# ===========================================================================
# state_commit
# ===========================================================================


@pytest.mark.asyncio
async def test_state_commit_returns_sha(workspace_root: Path) -> None:
    result = await state_commit(
        {
            "files": {"a.txt": b64(b"hello")},
            "deletes": [],
            "message": "add a\n\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    sha = result["sha"]
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


@pytest.mark.asyncio
async def test_state_commit_file_content_on_disk(workspace_root: Path) -> None:
    await state_commit(
        {
            "files": {"a.txt": b64(b"hello")},
            "deletes": [],
            "message": "add a\n\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    content = (workspace_root / ".state" / "a.txt").read_bytes()
    assert content == b"hello"


@pytest.mark.asyncio
async def test_state_commit_git_log_subject_and_message(workspace_root: Path) -> None:
    """Verify that git log shows the expected message and file is committed."""
    result = await state_commit(
        {
            "files": {"a.txt": b64(b"hello")},
            "deletes": [],
            "message": "add a\n\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    sha = result["sha"]
    log_out = subprocess.check_output(
        ["git", "-C", str(workspace_root / ".state"), "log", "-1", "--format=%H%n%B", sha],
        text=True,
    )
    assert log_out.startswith(sha)
    assert "X-Primer-Op: attach" in log_out


@pytest.mark.asyncio
async def test_state_commit_allow_empty_flag(workspace_root: Path) -> None:
    """allow_empty=True lets us commit with no file changes."""
    result = await state_commit(
        {
            "files": {},
            "deletes": [],
            "message": "no-op\n\nX-Primer-Op: attach\n",
            "allow_empty": True,
        },
        str(workspace_root),
    )
    assert len(result["sha"]) == 40


@pytest.mark.asyncio
async def test_state_commit_with_deletes(workspace_root: Path) -> None:
    """A second commit with deletes removes the file from the repo."""
    # First: add the file.
    await state_commit(
        {
            "files": {"a.txt": b64(b"hello")},
            "deletes": [],
            "message": "add a\n\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    assert (workspace_root / ".state" / "a.txt").exists()

    # Second: delete it.
    result = await state_commit(
        {
            "files": {},
            "deletes": ["a.txt"],
            "message": "rm a\n\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    assert len(result["sha"]) == 40
    assert not (workspace_root / ".state" / "a.txt").exists()


@pytest.mark.asyncio
async def test_state_commit_creates_parent_dirs(workspace_root: Path) -> None:
    """Files in subdirectories get their parent directories created."""
    await state_commit(
        {
            "files": {"sub/dir/b.txt": b64(b"nested")},
            "deletes": [],
            "message": "add nested\n\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    assert (workspace_root / ".state" / "sub" / "dir" / "b.txt").read_bytes() == b"nested"


# ===========================================================================
# state_read
# ===========================================================================


@pytest.mark.asyncio
async def test_state_read_returns_existing_files(workspace_root: Path) -> None:
    await state_commit(
        {
            "files": {"a.txt": b64(b"hello")},
            "deletes": [],
            "message": "add a\n\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    result = await state_read({"paths": ["a.txt", "missing.txt"]}, str(workspace_root))
    assert "files" in result
    files = result["files"]
    assert fromb64(files["a.txt"]) == b"hello"
    assert files["missing.txt"] is None


@pytest.mark.asyncio
async def test_state_read_all_missing(workspace_root: Path) -> None:
    result = await state_read({"paths": ["no.txt", "also/no.txt"]}, str(workspace_root))
    assert result == {"files": {"no.txt": None, "also/no.txt": None}}


@pytest.mark.asyncio
async def test_state_read_empty_paths(workspace_root: Path) -> None:
    result = await state_read({"paths": []}, str(workspace_root))
    assert result == {"files": {}}


# ===========================================================================
# state_history
# ===========================================================================


@pytest.mark.asyncio
async def test_state_history_returns_commits(workspace_root: Path) -> None:
    await state_commit(
        {
            "files": {"a.txt": b64(b"hello")},
            "deletes": [],
            "message": "first\n\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    result = await state_history({"limit": 10}, str(workspace_root))
    assert "commits" in result
    commits = result["commits"]
    # At least the commit we just made plus the initial empty commit.
    assert len(commits) >= 1
    # First commit (newest first) should be our named commit.
    subjects = [c["subject"] for c in commits]
    assert "first" in subjects


@pytest.mark.asyncio
async def test_state_history_record_fields(workspace_root: Path) -> None:
    """Each record has all expected fields."""
    await state_commit(
        {
            "files": {"a.txt": b64(b"hello")},
            "deletes": [],
            "message": "second\n\nX-Primer-Session: sess-abc\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    result = await state_history({"limit": 10}, str(workspace_root))
    commit = result["commits"][0]  # newest first
    for field in ("sha", "subject", "committed_at", "workspace_id", "session_id", "agent_id", "op", "tool", "call_id"):
        assert field in commit, f"missing field: {field}"
    assert len(commit["sha"]) == 40
    assert commit["subject"] == "second"
    assert commit["session_id"] == "sess-abc"
    assert commit["op"] == "attach"


@pytest.mark.asyncio
async def test_state_history_session_filter(workspace_root: Path) -> None:
    """session_id filter returns only matching commits."""
    await state_commit(
        {
            "files": {"x.txt": b64(b"x")},
            "deletes": [],
            "message": "for-sess-A\n\nX-Primer-Session: sess-A\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    await state_commit(
        {
            "files": {"y.txt": b64(b"y")},
            "deletes": [],
            "message": "for-sess-B\n\nX-Primer-Session: sess-B\nX-Primer-Op: attach\n",
            "allow_empty": False,
        },
        str(workspace_root),
    )
    # Filter to sess-A only.
    result = await state_history({"limit": 10, "session_id": "sess-A"}, str(workspace_root))
    commits = result["commits"]
    assert len(commits) == 1
    assert commits[0]["subject"] == "for-sess-A"
    assert commits[0]["session_id"] == "sess-A"


@pytest.mark.asyncio
async def test_state_history_limit(workspace_root: Path) -> None:
    """limit parameter caps returned results."""
    for i in range(5):
        await state_commit(
            {
                "files": {f"f{i}.txt": b64(f"v{i}".encode())},
                "deletes": [],
                "message": f"commit {i}\n\nX-Primer-Op: attach\n",
                "allow_empty": False,
            },
            str(workspace_root),
        )
    result = await state_history({"limit": 3}, str(workspace_root))
    assert len(result["commits"]) == 3


@pytest.mark.asyncio
async def test_state_history_newest_first(workspace_root: Path) -> None:
    """Commits are returned newest first."""
    for label in ("alpha", "beta", "gamma"):
        await state_commit(
            {
                "files": {f"{label}.txt": b64(label.encode())},
                "deletes": [],
                "message": f"{label}\n\nX-Primer-Op: attach\n",
                "allow_empty": False,
            },
            str(workspace_root),
        )
    result = await state_history({"limit": 10}, str(workspace_root))
    subjects = [c["subject"] for c in result["commits"]]
    gamma_idx = subjects.index("gamma")
    alpha_idx = subjects.index("alpha")
    assert gamma_idx < alpha_idx, "newest (gamma) should appear before oldest (alpha)"
