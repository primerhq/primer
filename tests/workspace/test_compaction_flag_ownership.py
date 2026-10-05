"""``begin_compaction`` / ``end_compaction`` on both state repos: the flag belongs to the window that set it.

The executor-level scenario (a cancelled turn's deferred close running after a later turn opened its window) is in
``tests/agent/test_compaction_window_flag_is_owned.py``; this pins the repo contract it rests on, for the local repo and the
sandbox repo alike.
"""

from __future__ import annotations

import pytest

from primer.workspace.local.state import LocalStateRepo
from primer.workspace.sandbox.state import SandboxStateRepo


@pytest.fixture(params=["local", "sandbox"])
def repo(request, tmp_path):
    if request.param == "local":
        return LocalStateRepo(tmp_path / ".state", workspace_id="ws-1")
    return SandboxStateRepo(object(), state_path=".state", workspace_id="ws-1")


def test_a_window_sets_the_flag_and_its_own_close_clears_it(repo):
    assert not repo.is_compacting("s1")
    token = repo.begin_compaction("s1")
    assert repo.is_compacting("s1")
    assert repo.end_compaction("s1", token) is True
    assert not repo.is_compacting("s1")


def test_every_window_gets_its_own_token(repo):
    first = repo.begin_compaction("s1")
    repo.end_compaction("s1", first)
    second = repo.begin_compaction("s1")
    assert second != first and second > first


def test_a_close_that_is_not_the_open_windows_is_a_no_op(repo):
    """The stale close of a turn that outlived its commit: another window is open, so the flag stays set and the caller is
    told not to drain the steers (they belong to the window that is still open)."""
    old = repo.begin_compaction("s1")
    new = repo.begin_compaction("s1")           # a later turn opened its own window before the old one was closed
    repo.add_pending_steer("s1", "a steer deferred in the new window")
    assert repo.end_compaction("s1", old) is False
    assert repo.is_compacting("s1"), "the new window's flag is still set"
    assert repo.peek_pending_steers("s1") == ["a steer deferred in the new window"]
    assert repo.end_compaction("s1", new) is True
    assert not repo.is_compacting("s1")


def test_the_open_window_can_still_close_after_a_stale_close_was_refused(repo):
    old = repo.begin_compaction("s1")
    new = repo.begin_compaction("s1")
    assert repo.end_compaction("s1", old) is False
    assert repo.end_compaction("s1", old) is False, "refused again, not consumed"
    assert repo.end_compaction("s1", new) is True


def test_a_close_when_no_window_is_open_may_go_on_to_drain(repo):
    """The later turn closed first and drained; the old close then finds nothing open and is allowed to retry any steers
    whose persistence failed (they stay queued by design)."""
    old = repo.begin_compaction("s1")
    new = repo.begin_compaction("s1")
    assert repo.end_compaction("s1", new) is True
    assert repo.end_compaction("s1", old) is True
    assert not repo.is_compacting("s1")


def test_sessions_do_not_share_windows(repo):
    a = repo.begin_compaction("s1")
    b = repo.begin_compaction("s2")
    assert repo.end_compaction("s1", a) is True
    assert repo.is_compacting("s2") and not repo.is_compacting("s1")
    assert repo.end_compaction("s2", b) is True
