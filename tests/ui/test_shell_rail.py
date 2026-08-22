"""Rail discipline (spec section 8): exactly three place-y lists.

Prohibited: a rail used as a utility junk drawer, and a sidebar that
forgets personalization. Both are checkable in source: the list set is a
frozen literal, and the prefs are keyed by the authenticated account.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-rail.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_exactly_three_lists() -> None:
    src = _src()
    m = re.search(r"var SH_RAIL_LISTS = \[([^\]]*)\]", src)
    assert m, "SH_RAIL_LISTS must be a literal so the rail cannot grow quietly"
    names = re.findall(r'"([a-z]+)"', m.group(1))
    assert names == ["sessions", "files", "attention"]


def test_global_utilities_are_not_in_the_rail() -> None:
    src = _src()
    for junk in ("Settings", "Docs", "Providers", "Admin"):
        assert junk not in src, f"{junk} belongs in the top bar, not the rail"


def test_personalization_is_keyed_by_account() -> None:
    src = _src()
    assert "SH_railPrefsKey" in src
    assert '"primer.shell.rail:"' in src
    assert "localStorage" in src
    for field in ("order", "hidden", "badgeStyle", "collapsed"):
        assert field in src, field


def test_sessions_are_frecency_ordered_with_status_and_nesting() -> None:
    src = _src()
    assert "parent_session_id" in src, "parent nesting is contract"
    assert "SH_statusLine" in src, "rail row chips render the same status string"
    assert "frecency" in src


def test_attention_counts_come_from_pending_yields() -> None:
    src = _src()
    assert "SH_api.pendingYields" in src
    assert 'data-testid="rail-attention"' in src


def test_rows_render_verbs_from_the_registry() -> None:
    """Dual-render rule: rail rows are registry-rendered affordances."""
    src = _src()
    assert 'forSurface("rail")' in src


def test_the_file_rail_reads_the_keys_the_tree_route_sends() -> None:
    """Regression: the file rail was empty for every workspace.

    GET /workspaces/{id}/files/tree answers {"path", "items"}, and each
    item carries "is_dir". The rail read "entries" and "type", neither of
    which that route has ever sent, so the list was empty however many
    files a workspace held and the empty state showed every time.

    Pinned against the route rather than restating its keys, so the two
    cannot drift apart again.
    """
    import inspect

    from primer.api.routers.workspaces import file_tree

    body = inspect.getsource(file_tree)
    assert '"items": items' in body
    assert '"is_dir"' in body

    src = _src()
    tree = src[src.index("function SH_FilesList"):]
    tree = tree[:tree.index("window.SH_")] if "window.SH_" in tree else tree
    assert "tree.data.items" in tree
    assert "entry.is_dir" in tree
    assert "tree.data.entries" not in tree


def test_a_folder_in_the_file_rail_opens_to_show_what_is_in_it() -> None:
    """Regression: the rail listed the root and nothing under it.

    The tree route answers one level at a time (recursive=false), so
    descending means asking for the child path. Without that a folder row
    did nothing at all, which is not a tree: the workspace root was the
    only thing the rail could ever show.
    """
    src = _src()
    assert "function SH_FilesSubtree(" in src
    subtree = src[src.index("function SH_FilesSubtree("):]
    subtree = subtree[:subtree.index("function SH_FilesList")]
    # Fetches the folder it was given, not the root again.
    assert "SH_api.filesTree(shell.wid, path, signal)" in subtree
    assert "tree.data.items" in subtree

    lst = src[src.index("function SH_FilesList"):]
    assert "toggle(entry.path)" in lst, "a folder row has to open the folder"
    assert "<SH_FilesSubtree path={entry.path} />" in lst
