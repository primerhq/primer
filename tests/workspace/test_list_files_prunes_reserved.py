"""A recursive listing for a non-admin prunes the reserved trees WHILE it walks (review of workspaces, W55c).

The walk was capped (offset + limit entries) and only then filtered for a non-admin, so a workspace whose ``.state/.git`` holds thousands of objects
could fill the whole capped walk with entries the page then dropped: the page came back short or empty while real files existed. ``list_files``
takes ``skip_reserved`` and never descends into the state and tmp trees; admins and ``skip_reserved=False`` list as before.
"""

from __future__ import annotations

import pytest

from tests.api.test_workspace_state_reads_real_backends import _local, _sandbox

pytestmark = pytest.mark.asyncio


def _seed_busy(root, n_git: int = 60) -> None:
    """A real tree of 4 entries and a state repo holding ``n_git`` objects."""
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print(1)\n")
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    objects = root / ".state" / ".git" / "objects"
    objects.mkdir(parents=True)
    for i in range(n_git):
        (objects / f"obj{i:03d}").write_text("x")
    (root / ".tmp").mkdir()
    (root / ".tmp" / "tool_1.txt").write_text("t")


def _paths(entries) -> list[str]:
    return sorted(e.path for e in entries)


@pytest.mark.parametrize("make", [_local, _sandbox], ids=["local", "sandbox"])
async def test_a_capped_walk_with_skip_reserved_is_full_of_real_entries(tmp_path, make):
    _seed_busy(tmp_path)
    ws = make(tmp_path)

    entries = await ws.list_files(".", recursive=True, max_entries=10, skip_reserved=True)

    assert _paths(entries) == ["a.txt", "b.txt", "src", "src/main.py"], "the cap must be spent on real files, not on the state repo"


@pytest.mark.parametrize("make", [_local, _sandbox], ids=["local", "sandbox"])
async def test_skip_reserved_never_returns_an_entry_under_a_reserved_tree(tmp_path, make):
    _seed_busy(tmp_path)

    entries = await make(tmp_path).list_files(".", recursive=True, skip_reserved=True)

    assert not [e.path for e in entries if e.path.split("/")[0] in (".state", ".tmp")]


@pytest.mark.parametrize("make", [_local, _sandbox], ids=["local", "sandbox"])
async def test_without_skip_reserved_the_walk_is_unchanged(tmp_path, make):
    """Characterization (what an admin gets): the reserved trees are listed."""
    _seed_busy(tmp_path, n_git=5)

    entries = await make(tmp_path).list_files(".", recursive=True)

    paths = _paths(entries)
    assert ".state" in paths and ".state/.git/objects/obj000" in paths and ".tmp/tool_1.txt" in paths and "src/main.py" in paths


@pytest.mark.parametrize("make", [_local, _sandbox], ids=["local", "sandbox"])
async def test_a_non_recursive_listing_and_a_subdirectory_walk_are_unaffected(tmp_path, make):
    _seed_busy(tmp_path, n_git=3)
    ws = make(tmp_path)

    top = await ws.list_files(".", recursive=False, skip_reserved=True)
    sub = await ws.list_files("src", recursive=True, skip_reserved=True)

    assert {"a.txt", "b.txt", "src"} <= {e.path for e in top}  # the router's post-filter drops the two reserved entries themselves
    assert _paths(sub) == ["src/main.py"]
