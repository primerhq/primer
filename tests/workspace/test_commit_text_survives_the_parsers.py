"""Whatever a commit message is built from, the history parsers read ONE record back with the trailers the writer put there (security ticket 01a11fbc-ffea,
#680 review round 1, B1-B4).

The state repo's commit message is read back by two parsers (``LocalStateRepo.history()`` and the in-pod runtime's ``_parse_log_records``) that split on
``git log --format`` separators: RS (``\\x1e``) between records, US (``\\x1f``) between fields, and lines for the trailers. A caller-controlled value that
holds any of those, or any control character git or the process would act on, forges or drops records and trailers: a workspace id (caller-chosen, written
by ``commit_arbitrary`` into ``X-Primer-Workspace``), a trailer value (a failure code, a provider's ``tool_call_id``) and the commit SUBJECT (a session
rename, a graph node id). These tests commit hostile text through every writer and read it back through BOTH parsers, for every character that is replaced:
a mutant that leaves one character alone (CR alone, US) passes a check of the raw message and fails here.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.model.workspace_session import AgentBinding, SessionInfo, SessionStatus
from primer.workspace.local.state import LocalStateRepo
from primer.workspace.sandbox.state import _build_arbitrary_message
from primer.workspace.state_helpers import build_message
from primer_runtime.ops import state_history

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

#: Every character that can end a line, separate a record or a field, or that a process refuses in an argument.
UNSAFE = [
    "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", "\x00", "\x01", "\x1b", "\t", "\x7f", "\x85", "\u2028", "\u2029",
]
IDS = [f"U+{ord(c):04X}" for c in UNSAFE]
REAL_FILE = "graphs/g/state.json"


async def _both(repo_root: Path, repo: LocalStateRepo, *, with_files: bool = False) -> tuple[list, list[dict]]:
    """The records ``LocalStateRepo.history()`` and the in-pod runtime's ``state_history`` read from the same repository."""
    local = await repo.history(limit=50, with_files=with_files)
    runtime = (await state_history({"limit": 50}, str(repo_root)))["commits"]
    return local, runtime


@pytest.fixture
async def repo(tmp_path: Path):
    state = LocalStateRepo(tmp_path / ".state", workspace_id="ws-test")
    await state.initialize()
    return tmp_path, state


@pytest.mark.asyncio
@pytest.mark.parametrize("char", UNSAFE, ids=IDS)
async def test_a_trailer_value_with_the_character_stays_one_record_in_both_parsers(repo, char) -> None:
    root, state = repo
    hostile = f"ok{char}X-Primer-Workspace: other{char}X-Primer-Session: forged"

    await state.commit_arbitrary(summary="graph g: state", files={REAL_FILE: "{}"}, trailers={"X-Primer-Graph-Ended-Detail": hostile})

    local, runtime = await _both(root, state)
    assert len(local) == 1 and len(runtime) == 1, (local, runtime)
    assert local[0].workspace_id == "ws-test" and runtime[0]["workspace_id"] == "ws-test"
    assert local[0].session_id is None and runtime[0]["session_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("char", UNSAFE, ids=IDS)
async def test_a_workspace_id_with_the_character_cannot_forge_a_trailer(tmp_path: Path, char) -> None:
    """The workspace id is caller-chosen and unvalidated; ``commit_arbitrary`` wrote it raw into ``X-Primer-Workspace``."""
    state = LocalStateRepo(tmp_path / ".state", workspace_id=f"ws{char}X-Primer-Session: forged{char}X-Primer-Agent: forged")
    await state.initialize()

    await state.commit_arbitrary(summary="graph g: state", files={REAL_FILE: "{}"})

    local, runtime = await _both(tmp_path, state)
    assert len(local) == 1 and len(runtime) == 1, (local, runtime)
    assert local[0].session_id is None and runtime[0]["session_id"] is None
    assert local[0].agent_id is None and runtime[0]["agent_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("char", UNSAFE, ids=IDS)
async def test_a_subject_with_the_character_is_one_line_in_both_parsers(repo, char) -> None:
    root, state = repo

    await state.commit_arbitrary(summary=f"rename[abc]: a{char}b", files={REAL_FILE: "{}"})

    local, runtime = await _both(root, state)
    assert len(local) == 1 and len(runtime) == 1, (local, runtime)
    assert char not in local[0].subject and char not in runtime[0]["subject"]
    assert local[0].subject.startswith("rename[abc]: a") and local[0].subject.endswith("b")


@pytest.mark.asyncio
async def test_a_subject_cannot_forge_a_whole_record(repo) -> None:
    """A rename (any authenticated user) with RS and US in it forged a complete record in both parsers and hid the real commit."""
    root, state = repo
    forged = "x\x1e" + "f" * 40 + "\x1fforged\x1f2026-01-01T00:00:00+00:00\x1fX-Primer-Workspace: other\x1f"

    sha = await state.commit_arbitrary(summary=f"rename[abc]: {forged}", files={REAL_FILE: "{}"})

    local, runtime = await _both(root, state)
    assert [c.sha for c in local] == [sha] and [c["sha"] for c in runtime] == [sha]
    assert local[0].workspace_id == "ws-test" and runtime[0]["workspace_id"] == "ws-test"


@pytest.mark.asyncio
async def test_one_us_in_a_subject_does_not_break_history_for_the_workspace(repo) -> None:
    """One US made ``history()`` raise for every window containing the commit, so the workspace log endpoint and the log tool failed from then on."""
    root, state = repo
    await state.commit_arbitrary(summary="before", files={"a.txt": "1"})
    await state.commit_arbitrary(summary="rename[abc]: a\x1fb", files={"b.txt": "2"})
    await state.commit_arbitrary(summary="after", files={"c.txt": "3"})

    local, runtime = await _both(root, state)

    assert [c.subject for c in local][0] == "after" and len(local) == 3
    assert len(runtime) == 3


@pytest.mark.asyncio
async def test_a_us_in_a_trailer_value_cannot_forge_numstat_files(repo) -> None:
    """US is the parsers' FIELD separator: it forged ``--numstat`` rows (a file never touched listed) or dropped the trailers after it."""
    root, state = repo
    await state.commit_arbitrary(
        summary="graph g: state", files={REAL_FILE: "{}"},
        trailers={"X-Primer-Graph-Ended-Detail": "x\x1f1\t0\tnever/touched.txt", "X-Primer-Graph-Status": "failed"},
    )

    local, _runtime = await _both(root, state, with_files=True)

    assert [f.path for f in local[0].files] == [REAL_FILE], local[0].files


@pytest.mark.asyncio
async def test_a_nul_in_a_value_does_not_fail_the_commit(repo) -> None:
    """A NUL cannot be passed in an argument: the commit used to fail (500) instead of writing the sanitised value."""
    root, state = repo

    await state.commit_arbitrary(summary="a\x00b", files={REAL_FILE: "{}"}, trailers={"X-Primer-Graph-Ended-Detail": "x\x00y"})

    local, runtime = await _both(root, state)
    assert len(local) == 1 and "\x00" not in local[0].subject and "\x00" not in runtime[0]["subject"]


# ---- the sandbox backend's builder and the session-scoped builder, committed and read back through the parsers ----------------------------------


def _commit_message(tmp_path: Path, message: str) -> tuple[Path, str]:
    """Commit ``message`` verbatim into a fresh git repo at ``<tmp>/.state`` and return (workspace root, sha)."""
    state = tmp_path / ".state"
    state.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@t"]):
        subprocess.run(["git", "-C", str(state), *args], check=True)
    subprocess.run(["git", "-C", str(state), "commit", "--allow-empty", "-q", "-m", message], check=True)
    sha = subprocess.check_output(["git", "-C", str(state), "rev-parse", "HEAD"], text=True).strip()
    return tmp_path, sha


@pytest.mark.asyncio
@pytest.mark.parametrize("char", UNSAFE, ids=IDS)
async def test_the_sandbox_arbitrary_message_reads_back_as_one_record(tmp_path: Path, char) -> None:
    message = _build_arbitrary_message(
        subject=f"graph g: a{char}b", workspace_id=f"ws-test{char}X-Primer-Session: forged",
        trailers={"X-Primer-Graph-Ended-Detail": f"ok{char}X-Primer-Agent: forged"},
    )
    assert "\x00" not in message, "a NUL cannot be passed to git in an argument: the builder must already have removed it"
    root, sha = _commit_message(tmp_path, message)

    commits = (await state_history({"limit": 10}, str(root)))["commits"]

    assert [c["sha"] for c in commits] == [sha]
    assert commits[0]["session_id"] is None and commits[0]["agent_id"] is None
    assert char not in commits[0]["subject"]


@pytest.mark.asyncio
@pytest.mark.parametrize("char", UNSAFE, ids=IDS)
async def test_the_session_commit_message_reads_back_as_one_record(tmp_path: Path, char) -> None:
    """Every value of the message is hostile, the workspace id and the agent id included: both are caller-chosen and written into EVERY session commit."""
    message = build_message(
        subject=f"user[abc]: a{char}b", workspace_id=f"ws-test{char}X-Primer-Session: forged", session_id=f"sess-1{char}X-Primer-Op: forged",
        agent_id=f"ag{char}X-Primer-Workspace: other", op="tool",
        tool=f"fs__read{char}X-Primer-Op: forged", call_id=f"call_0{char}X-Primer-Workspace: other",
    )
    assert "\x00" not in message
    root, sha = _commit_message(tmp_path, message)

    commits = (await state_history({"limit": 10}, str(root)))["commits"]

    assert [c["sha"] for c in commits] == [sha]
    assert commits[0]["workspace_id"] == "ws-test X-Primer-Session: forged", "the workspace id is ONE trailer value, not a trailer plus a forged one"
    assert commits[0]["session_id"] == "sess-1 X-Primer-Op: forged"
    assert commits[0]["agent_id"] == "ag X-Primer-Workspace: other" and commits[0]["op"] == "tool"
    assert char not in commits[0]["subject"]


@pytest.mark.asyncio
@pytest.mark.parametrize("char", UNSAFE, ids=IDS)
async def test_a_session_commit_of_the_local_repo_with_a_hostile_workspace_id_reads_back_as_one_record(tmp_path: Path, char) -> None:
    """``LocalStateRepo.commit`` (not ``commit_arbitrary``) writes the workspace id and the agent id into every session commit through ``build_message``."""
    state = LocalStateRepo(tmp_path / ".state", workspace_id=f"ws{char}X-Primer-Session: forged")
    await state.initialize()
    now = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
    info = SessionInfo(
        session_id="sess-1", agent_id=f"ag{char}X-Primer-Op: forged", workspace_id="ws", status=SessionStatus.RUNNING, started_at=now, last_activity_at=now,
    )
    await state.create_session(info, AgentBinding(agent_id=info.agent_id, agent_name="n"))

    sha = await state.commit("sess-1", summary="sess-1: hello", op="message", files={"messages.jsonl": "{}\n"})

    local, runtime = await _both(tmp_path, state)
    assert sha in {c.sha for c in local} and sha in {c["sha"] for c in runtime}
    assert len(local) == 2 and len(runtime) == 2, "the attach commit and this one, each ONE record"
    assert {c.op for c in local} == {"attach", "message"} and {c["op"] for c in runtime} == {"attach", "message"}, (local, runtime)
