"""Per-file line deltas on ``history(with_files=True)``.

The Studio's Changes view needs a file list with ``+n/-m`` per commit. Before
this, the only way to get one was ``show_commit`` per commit -- three git
invocations each, so ~150 processes for a 50-commit page. ``--numstat`` rides
along on the existing ``git log`` instead, at the cost of one format change.

That format change is the risk this file is really guarding: the record
separator had to move to the FRONT of the format string, because git prints
numstat AFTER the formatted line and a trailing separator would file each
commit's numstat under the next commit. These tests pin both the new field and
the untouched header parsing that shares the format with it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from primer.model.workspace import CommitFile
from primer.workspace.local.state import LocalStateRepo, _parse_numstat

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


@pytest.fixture
async def repo(tmp_path: Path) -> LocalStateRepo:
    r = LocalStateRepo(tmp_path / ".state", workspace_id="ws-test")
    await r.initialize()
    return r


# ---------------------------------------------------------------------------
# The default is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_is_none_when_not_requested(repo: LocalStateRepo) -> None:
    # None means "not requested / cannot tell", and must not become [].
    await repo.commit_arbitrary(summary="a", files={"a.txt": "x\n"})
    commits = await repo.history(limit=10)
    assert commits
    assert all(c.files is None for c in commits)


@pytest.mark.asyncio
async def test_header_fields_survive_the_format_change(repo: LocalStateRepo) -> None:
    # The numstat field shares the format string with the header, so a mistake
    # there corrupts sha/subject/date/trailers for every existing caller.
    await repo.commit_arbitrary(
        summary="feat: a subject line",
        files={"a.txt": "x\n"},
        trailers={"X-Primer-Session": "sess-1", "X-Primer-Op": "tool_call"},
    )
    for with_files in (False, True):
        commits = await repo.history(limit=10, with_files=with_files)
        head = commits[0]
        assert len(head.sha) == 40, with_files
        assert head.subject == "feat: a subject line", with_files
        assert head.committed_at is not None, with_files
        assert head.session_id == "sess-1", with_files
        assert head.op == "tool_call", with_files


@pytest.mark.asyncio
async def test_multiple_trailers_still_all_parse(repo: LocalStateRepo) -> None:
    # The trailer block is multi-line and sits immediately before the numstat
    # field; an off-by-one separator would swallow the second trailer.
    await repo.commit_arbitrary(
        summary="s",
        files={"a.txt": "x\n"},
        trailers={
            "X-Primer-Session": "sess-9",
            "X-Primer-Agent": "agent-9",
            "X-Primer-Tool": "write",
        },
    )
    head = (await repo.history(limit=1, with_files=True))[0]
    assert head.session_id == "sess-9"
    assert head.agent_id == "agent-9"
    assert head.tool == "write"


# ---------------------------------------------------------------------------
# With files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_additions_are_counted_on_a_new_file(repo: LocalStateRepo) -> None:
    await repo.commit_arbitrary(summary="add", files={"a.txt": "1\n2\n3\n"})
    head = (await repo.history(limit=1, with_files=True))[0]
    assert head.files is not None
    entry = next(f for f in head.files if f.path == "a.txt")
    assert entry.additions == 3
    assert entry.deletions == 0
    assert entry.binary is False


@pytest.mark.asyncio
async def test_a_modification_counts_both_directions(repo: LocalStateRepo) -> None:
    await repo.commit_arbitrary(summary="first", files={"a.txt": "1\n2\n3\n"})
    await repo.commit_arbitrary(summary="second", files={"a.txt": "1\nX\n3\n4\n"})
    head = (await repo.history(limit=1, with_files=True))[0]
    entry = next(f for f in head.files if f.path == "a.txt")
    assert entry.additions == 2
    assert entry.deletions == 1


@pytest.mark.asyncio
async def test_a_deletion_is_reported(repo: LocalStateRepo) -> None:
    await repo.commit_arbitrary(summary="first", files={"a.txt": "1\n2\n"})
    await repo.commit_arbitrary(summary="drop", delete_files=["a.txt"])
    head = (await repo.history(limit=1, with_files=True))[0]
    entry = next(f for f in head.files if f.path == "a.txt")
    assert entry.additions == 0
    assert entry.deletions == 2


@pytest.mark.asyncio
async def test_every_file_in_a_multi_file_commit_appears(repo: LocalStateRepo) -> None:
    await repo.commit_arbitrary(
        summary="three",
        files={"a.txt": "1\n", "b.txt": "1\n2\n", "sub/c.txt": "1\n2\n3\n"},
    )
    head = (await repo.history(limit=1, with_files=True))[0]
    got = {f.path: f.additions for f in head.files}
    assert got == {"a.txt": 1, "b.txt": 2, "sub/c.txt": 3}


@pytest.mark.asyncio
async def test_a_binary_file_is_flagged_not_zero_counted(repo: LocalStateRepo) -> None:
    # git reports '-' for both counts. Reporting a bare 0/0 would read as
    # "changed nothing" rather than "lines are not the unit here".
    await repo.commit_arbitrary(summary="bin", files={"blob.bin": b"\x00\x01\x02binary"})
    head = (await repo.history(limit=1, with_files=True))[0]
    entry = next(f for f in head.files if f.path == "blob.bin")
    assert entry.binary is True
    assert entry.additions == 0
    assert entry.deletions == 0


@pytest.mark.asyncio
async def test_each_commit_keeps_its_own_files(repo: LocalStateRepo) -> None:
    # THE regression this format change exists to avoid: with a trailing
    # separator, commit N's numstat lands on commit N+1.
    await repo.commit_arbitrary(summary="first", files={"only-in-first.txt": "1\n"})
    await repo.commit_arbitrary(summary="second", files={"only-in-second.txt": "1\n2\n"})
    commits = await repo.history(limit=10, with_files=True)
    by_subject = {c.subject: c for c in commits}
    first_paths = {f.path for f in by_subject["first"].files}
    second_paths = {f.path for f in by_subject["second"].files}
    assert "only-in-first.txt" in first_paths
    assert "only-in-first.txt" not in second_paths
    assert "only-in-second.txt" in second_paths
    assert "only-in-second.txt" not in first_paths


@pytest.mark.asyncio
async def test_with_files_still_honours_the_session_filter(repo: LocalStateRepo) -> None:
    await repo.commit_arbitrary(
        summary="mine", files={"a.txt": "1\n"},
        trailers={"X-Primer-Session": "sess-keep"},
    )
    await repo.commit_arbitrary(
        summary="theirs", files={"b.txt": "1\n"},
        trailers={"X-Primer-Session": "sess-drop"},
    )
    commits = await repo.history(session_id="sess-keep", limit=10, with_files=True)
    assert [c.subject for c in commits] == ["mine"]
    assert {f.path for f in commits[0].files} == {"a.txt"}


@pytest.mark.asyncio
async def test_with_files_still_honours_the_limit(repo: LocalStateRepo) -> None:
    for i in range(5):
        await repo.commit_arbitrary(summary=f"c{i}", files={f"f{i}.txt": "1\n"})
    commits = await repo.history(limit=2, with_files=True)
    assert len(commits) == 2


@pytest.mark.asyncio
async def test_a_rename_is_reported_as_paths_that_exist(repo: LocalStateRepo) -> None:
    # --no-renames is deliberate: rename detection writes "old => new", which
    # is not a path any other endpoint accepts. Both sides must be real paths.
    await repo.commit_arbitrary(summary="first", files={"old.txt": "1\n2\n3\n"})
    await repo.commit_arbitrary(
        summary="move", files={"new.txt": "1\n2\n3\n"}, delete_files=["old.txt"]
    )
    head = (await repo.history(limit=1, with_files=True))[0]
    paths = {f.path for f in head.files}
    assert paths == {"old.txt", "new.txt"}
    assert not any("=>" in p for p in paths)


# ---------------------------------------------------------------------------
# The numstat parser in isolation
# ---------------------------------------------------------------------------


def test_numstat_parser_handles_a_path_containing_a_tab() -> None:
    # The split is bounded to 2, so a tab inside the path stays in the path.
    out = _parse_numstat("1\t2\tdir/we\taird.txt\n")
    assert out == [CommitFile(path="dir/we\taird.txt", additions=1, deletions=2)]


def test_numstat_parser_skips_blank_and_malformed_lines() -> None:
    # One odd line must never take out a whole page of history.
    out = _parse_numstat("\n\n5\t1\tgood.txt\nnot-a-numstat-line\n\t\t\n")
    assert [f.path for f in out] == ["good.txt"]


def test_numstat_parser_skips_non_integer_counts() -> None:
    out = _parse_numstat("x\ty\tbad.txt\n3\t0\tgood.txt\n")
    assert [f.path for f in out] == ["good.txt"]


def test_numstat_parser_on_an_empty_block_is_empty() -> None:
    assert _parse_numstat("") == []
    assert _parse_numstat("\n\n") == []


def test_commit_file_rejects_unknown_fields() -> None:
    # extra="forbid", so a backend that starts sending a differently-named
    # field fails loudly instead of dropping it.
    with pytest.raises(Exception):
        CommitFile(path="a", additions=1, deletions=0, adds=5)  # type: ignore[call-arg]


def test_commit_file_rejects_negative_counts() -> None:
    with pytest.raises(Exception):
        CommitFile(path="a", additions=-1, deletions=0)
