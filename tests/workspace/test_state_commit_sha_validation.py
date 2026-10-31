"""A revision that reaches git is a hex object name, never an option (review of workspaces, W13).

``GET /workspaces/{id}/commit/{sha}`` handed the sha straight to ``git show``: ``--output=x`` made git write a file at the ``.state`` root for a
read-only user. The repo refuses anything that is not 4 to 64 hex digits before any git process starts, the route's pattern answers 422, and the
revision is followed by ``--`` so it can never be read as a path.
"""

from __future__ import annotations

import pytest

from primer.workspace.local.state import LocalStateRepo

HOSTILE = ["--output=pwned.txt", "--output", "-p", "--exec-path=/tmp", "HEAD", "main", "a b", "abc", "zzzzzzz", "abc/../def", "abcdef0:file", ""]


@pytest.fixture
async def repo(tmp_path):
    r = LocalStateRepo(tmp_path / ".state", workspace_id="ws-1")
    await r.initialize()
    await r.commit_arbitrary(summary="seed", files={"a.txt": "hello"})
    return r


@pytest.mark.parametrize("sha", HOSTILE)
async def test_show_commit_refuses_anything_that_is_not_hex_and_writes_nothing(repo, tmp_path, sha):
    before = sorted(p.name for p in (tmp_path / ".state").iterdir())

    with pytest.raises(ValueError):
        await repo.show_commit(sha)

    assert sorted(p.name for p in (tmp_path / ".state").iterdir()) == before, "git wrote a file"


@pytest.mark.parametrize("sha", HOSTILE)
async def test_read_at_refuses_anything_that_is_not_hex(repo, sha):
    with pytest.raises(ValueError):
        await repo.read_at(sha, "a.txt")


async def test_a_real_full_or_abbreviated_sha_still_works(repo):
    await repo.commit_arbitrary(summary="second", files={"b.txt": "world"})  # a root commit lists no files (diff-tree without --root)
    full = (await repo.history(limit=1))[0].sha
    for sha in (full, full[:7], full[:12].upper()):
        shown = await repo.show_commit(sha)
        assert shown["subject"] == "second" and [f["path"] for f in shown["files"]] == ["b.txt"], sha
        assert "world" in shown["files"][0]["patch"]
    assert await repo.read_at(full, "b.txt") == b"world"


async def test_an_unknown_but_well_formed_sha_is_still_not_found(repo):
    with pytest.raises(FileNotFoundError):
        await repo.show_commit("0" * 40)


async def test_a_session_or_agent_filter_is_a_literal_not_a_pattern(repo):
    """The ``--grep`` filters are anchored literals: ``.*`` must not match every session's commits (it used to be spliced in as a pattern)."""
    await repo.commit_arbitrary(summary="s1", files={"b.txt": "1"}, trailers={"X-Primer-Session": "sess-1", "X-Primer-Agent": "ag-1"})
    await repo.commit_arbitrary(summary="s2", files={"c.txt": "2"}, trailers={"X-Primer-Session": "sess-2", "X-Primer-Agent": "ag-1"})

    assert [c.subject for c in await repo.history(session_id="sess-1")] == ["s1"]
    assert await repo.history(session_id="sess-.*") == []
    assert await repo.history(session_id="sess-1", agent_id="ag.1") == []
