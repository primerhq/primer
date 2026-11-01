"""A symlink into the reserved trees does not get past the reserved-tree rule on a sandbox workspace (review of workspaces, W55d).

``SandboxWorkspace.reserved_tree_of`` classified the path STRING while the runtime follows symlinks: after ``ln -s .state x`` (an exec in the
workspace) a REST read, write or delete of ``x/...`` reached the state tree past the A-22 rule. The path is now also resolved on the runtime side (its
``stat`` answers the resolved path) before the classification; a link to an ordinary directory still works, and an admin may still read the reserved
trees for debugging.
"""

from __future__ import annotations

import pytest

from primer.model.except_ import BadRequestError
from primer.workspace.reserved import reserved_tree_for_resolved
from tests.api.test_workspace_state_reads_real_backends import _read_as_user, _sandbox, _seed

pytestmark = pytest.mark.asyncio

LINKED = "x/sessions/s1/messages.jsonl"


@pytest.fixture
def ws(tmp_path):
    _seed(tmp_path)
    (tmp_path / "x").symlink_to(".state")  # what `ln -s .state x` leaves behind
    (tmp_path / "s").symlink_to("src")  # an ordinary link, inside the workspace, to a non-reserved directory
    return _sandbox(tmp_path)


async def test_a_users_read_through_a_link_into_the_state_tree_is_refused(ws):
    resp = await _read_as_user(ws, LINKED)

    assert resp.status_code == 403, resp.text
    assert b"Traceback" not in resp.content


async def test_the_resolving_classifier_names_the_tree_behind_the_link(ws):
    assert await reserved_tree_for_resolved(ws, LINKED) == ".state"
    assert await reserved_tree_for_resolved(ws, "x") == ".state"
    assert await reserved_tree_for_resolved(ws, "x/not-there-yet.txt") == ".state", "a file to be created under the link"


async def test_a_write_through_the_link_is_refused_and_writes_nothing(ws, tmp_path):
    with pytest.raises(BadRequestError):
        await ws.write_file("x/planted.txt", b"pwned")

    assert not (tmp_path / ".state" / "planted.txt").exists()


async def test_a_delete_and_a_mkdir_and_a_move_through_the_link_are_refused(ws, tmp_path):
    with pytest.raises(BadRequestError):
        await ws.delete_file(LINKED)
    with pytest.raises(BadRequestError):
        await ws.make_dir("x/newdir")
    assert (tmp_path / ".state" / "sessions" / "s1" / "messages.jsonl").exists()
    assert not (tmp_path / ".state" / "newdir").exists()


async def test_a_link_to_an_ordinary_directory_still_reads_and_writes(ws, tmp_path):
    resp = await _read_as_user(ws, "s/main.py")
    assert resp.status_code == 200, resp.text

    await ws.write_file("s/new.txt", b"fine")

    assert (tmp_path / "src" / "new.txt").read_bytes() == b"fine"
    assert await reserved_tree_for_resolved(ws, "s/main.py") is None


async def test_the_lexical_rules_are_unchanged(ws):
    assert await reserved_tree_for_resolved(ws, ".state/sessions/s1/messages.jsonl") == ".state"
    assert await reserved_tree_for_resolved(ws, "src\\main.py") is None
    assert await reserved_tree_for_resolved(ws, "does/not/exist.txt") is None
