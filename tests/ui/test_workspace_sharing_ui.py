"""ACL-P2-T10: the console's workspace sharing (plan
docs/superpowers/plans/2026-10-30-access-control-p2-workspaces.md, Task 10;
access spec 6.2 and 9).

The backend (already on feat/access-p2) owns and shares workspaces: every
row carries ``access``, the list serves only readable rows, and
``GET /v1/workspaces/{id}/access`` answers ``{owners, grants, public,
public_tools, rev, your_level}``; writes answer 403 code access_denied at a
read level and 404 on unreadable rows. This file pins the CONSOLE side
against that contract:

* ``ui/components/workspaces.jsx``: the workspace Settings overlay is a
  shareable surface - a Share button (``data-testid="ws-share"``,
  ``data-verb="entity.share"``) beside the header controls, the share-seam
  subscription (``SHR_useShareOpen``), and ``SHR_ShareDialog`` mounted with
  ``plural="workspaces"`` and no public-tools section.
* ``ui/components/console/nv-rail.jsx``: the rail - the workspace switcher,
  since the topbar dropdown retired - groups its rows **Mine** (the viewer
  owns the row), **Shared with me** (a grant names the viewer) and
  **Public** (a public row, or one no clause names: an admin sees every
  row). The split is ``NV_wsGroupOf``, run here in MiniRacer against the
  real source.
* ``ui/components/console/nv-shell.jsx``: the open workspace's level is read
  through ``GET /workspaces/{id}/access`` (``useResource``, key
  ``workspace-access:<wid>``) and exposed on the console context as
  ``wsLevel`` / ``wsReadonly`` (true for a ``read`` level only: admins hold
  ``owner``, so only a genuine read grant goes read-only).
* ``ui/components/console/nv-session-doc.jsx``: at read level the composer
  (input, send, mic, stop) and the session controls (rename, park, end,
  delete) stay drawn but disabled, each carrying the reason.
* ``ui/components/console/nv-files-sidebar.jsx``: new file, upload and the
  rename/delete file actions are disabled with the reason.
* ``ui/components/console/nv-chrome.jsx``: the terminal toggle is disabled
  with the reason.
* ``ui/foundation/api.js``: a 403 with code ``access_denied`` renders
  through ``readRefusal`` with a human sentence - the server's own words
  when it sent them, a bare sentence otherwise.

The JSX wiring is source-checked against the real lines (the house style of
tests/ui/test_share_dialog.py); the two pure decisions (the group split, the
refusal sentence) run in MiniRacer against the real source.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

INDEX = (UI / "index.html").read_text(encoding="utf-8")
WORKSPACES_SRC = (UI / "components" / "workspaces.jsx").read_text(encoding="utf-8")
RAIL_SRC = (UI / "components" / "console" / "nv-rail.jsx").read_text(encoding="utf-8")
SHELL_SRC = (UI / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
SESSION_SRC = (UI / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
FILES_SRC = (UI / "components" / "console" / "nv-files-sidebar.jsx").read_text(encoding="utf-8")
CHROME_SRC = (UI / "components" / "console" / "nv-chrome.jsx").read_text(encoding="utf-8")
RAIL_PATH = UI / "components" / "console" / "nv-rail.jsx"
API_PATH = UI / "foundation" / "api.js"

_OPEN_CONTEXTS: list = []


def _after(src: str, anchor: str, window: int = 300) -> str:
    i = src.index(anchor)
    return src[i : i + window]


def _transpiled(path: Path) -> str:
    from ui._mini_react import transpile

    return transpile(path)


def _mini_ctx(src: str):
    from ui._mini_react import mini_react_context

    ctx = mini_react_context(src)
    _OPEN_CONTEXTS.append(ctx)
    return ctx


def _close_isolates():
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


# ===========================================================================
# The shareable surface (workspaces.jsx)
# ===========================================================================


def test_workspace_settings_is_a_shareable_surface() -> None:
    """The Settings overlay draws the Share button, subscribes to the share
    seam so the palette's entity.share verb reaches it, and opens the dialog
    with the workspaces plural - no public-tools section (a workspace's
    public switch is its whole row, not per-tool)."""
    btn = _after(WORKSPACES_SRC, 'data-testid="ws-share"', 240)
    assert 'data-verb="entity.share"' in btn, "the button is not the share verb"
    assert "window.SHR_useShareOpen" in WORKSPACES_SRC, "the seam subscription is missing"
    dlg = _after(WORKSPACES_SRC, "<SHR_ShareDialog", 400)
    assert 'plural="workspaces"' in dlg, "the dialog must fetch /workspaces/{id}/access"
    assert "allowPublicTools" not in dlg, "workspaces have no per-tool public switch"


def test_the_share_dialog_loads_before_the_workspace_surface() -> None:
    """SHR_ShareDialog and the seam globals are read at open time, so the
    dialog script must be loaded before workspaces.jsx runs (ui/index.html
    discipline: shared components before page components)."""
    dlg = INDEX.index('src="components/shared/share-dialog.jsx"')
    ws = INDEX.index('src="components/workspaces.jsx"')
    assert dlg < ws, "share-dialog.jsx must load before workspaces.jsx"


# ===========================================================================
# The rail grouping (nv-rail.jsx)
# ===========================================================================


def test_ws_group_of_splits_mine_shared_public() -> None:
    """NV_wsGroupOf against the real source: ownership outranks everything;
    a grant outranks public; public (or no clause at all - the admin's view
    of rows they hold nothing on, and a pre-Phase-2 row with no access
    block) lands in Public; no viewer id (signed out) names nobody."""
    ctx = _mini_ctx(_transpiled(RAIL_PATH))

    def grp(row: dict, viewer) -> str:
        return json.loads(
            ctx.eval(f"JSON.stringify(NV_wsGroupOf({json.dumps(row)}, {json.dumps(viewer)}))")
        )

    assert grp({"access": {"owners": ["u1"], "grants": {}, "public": False}}, "u1") == "mine"
    assert grp({"access": {"owners": ["u2"], "grants": {"u1": "full"}, "public": False}}, "u1") == "shared"
    assert grp({"access": {"owners": ["u2"], "grants": {"u1": "read"}, "public": False}}, "u1") == "shared"
    assert grp({"access": {"owners": [], "grants": {}, "public": True}}, "u1") == "public"
    assert grp({"access": {"owners": ["u1"], "grants": {}, "public": True}}, "u1") == "mine"
    assert grp({"access": {"owners": ["u1"], "grants": {"u9": "read"}, "public": True}}, "u9") == "shared"
    assert grp({}, "u1") == "public"
    assert grp({"access": {"owners": ["u1"], "grants": {}, "public": False}}, None) == "public"
    assert grp({"access": {"owners": ["u1"], "grants": {}, "public": False}}, "u9") == "public"


def test_the_rail_groups_the_workspace_rows() -> None:
    """The three group labels in display order, the group header's testid,
    and the viewer id read from the console context's principal."""
    assert (
        '["mine", "Mine"], ["shared", "Shared with me"], ["public", "Public"]' in RAIL_SRC
    ), "the Mine / Shared with me / Public groups in that order are missing"
    assert 'nv-rail-ws-group:' in RAIL_SRC, "the group header carries no testid"
    assert "NV_wsGroupOf(" in RAIL_SRC, "the rows are not grouped by NV_wsGroupOf"
    assert "con.viewer" in RAIL_SRC, "the viewer id must come from the console context"


# ===========================================================================
# The read-only level (nv-shell.jsx)
# ===========================================================================


def test_the_console_exposes_the_open_workspace_level() -> None:
    """One read per open workspace (useResource, key workspace-access:<wid>),
    from the access sub-resource; wsReadonly is true for a read level ONLY
    (owner/full keep every control; admins hold owner), and the disabled
    controls all carry the same short reason."""
    read = _after(SHELL_SRC, '"workspace-access:"', 600)
    assert "/workspaces/" in read and "/access" in read, (
        "the level must come from GET /workspaces/{id}/access"
    )
    assert "your_level" in SHELL_SRC, "the level is the view's your_level"
    assert "wsReadonly" in SHELL_SRC, "the console context exposes wsReadonly"
    assert "Read-only: you can view this workspace" in SHELL_SRC, (
        "the read-only reason the disabled controls carry"
    )


# ===========================================================================
# The disabled controls, each with the reason
# ===========================================================================


def test_the_composer_is_read_only_with_a_reason() -> None:
    """The input, the send button and the reason line: drawn, not hidden;
    disabled at read level with the note as its words."""
    ta = _after(SESSION_SRC, 'data-testid="nv-composer-input"', 260)
    assert "wsRo" in ta and "disabled" in ta, "the composer input is not disabled at read level"
    send = _after(SESSION_SRC, 'data-testid="nv-send"', 220)
    assert "wsRo" in send, "the send button is not disabled at read level"
    assert 'data-testid="nv-composer-readonly"' in SESSION_SRC, (
        "the reason line is missing - a read-only composer must say why"
    )


def test_the_session_controls_are_read_only() -> None:
    """Rename, park, end, delete and the running-turn stop stay in the menu,
    disabled with the reason at read level (a read grant is not a drive)."""
    for tid in (
        "nv-session-rename",
        "nv-session-park",
        "nv-session-end",
        "nv-session-delete",
        "nv-stop",
    ):
        assert "wsRo" in _after(SESSION_SRC, f'data-testid="{tid}"', 300), (
            f"the {tid} control is not disabled at read level"
        )


def test_the_file_writes_are_read_only() -> None:
    """New file and upload in the header; rename and delete in the row's
    menu - the writes are disabled at read level (reads - open, download,
    copy path - stay live)."""
    assert "wsRo" in _after(FILES_SRC, 'data-testid="nv-file-new"', 260)
    assert "wsRo" in _after(FILES_SRC, 'data-testid="nv-file-upload"', 260)
    assert "wsRo" in _after(FILES_SRC, 'label: "Rename"', 200), (
        "the rename row is not disabled at read level"
    )
    assert "wsRo" in _after(FILES_SRC, 'label: "Delete"', 140), (
        "the delete row is not disabled at read level"
    )


def test_the_terminal_toggle_is_read_only() -> None:
    assert "wsRo" in _after(CHROME_SRC, 'data-testid="nv-toggle-terminal"', 400), (
        "the terminal toggle is not disabled at read level"
    )


# ===========================================================================
# The 403 answer (api.js)
# ===========================================================================


def test_access_denied_reads_as_a_human_sentence() -> None:
    """readRefusal against the real source: with the server's own words
    (the require() sentence) it says them; bare (code only) it falls back
    to the access_denied sentence, not the code or the HTTP title."""
    ctx = _mini_ctx(_transpiled(API_PATH))

    def refusal(spec: str) -> dict:
        return json.loads(
            ctx.eval("JSON.stringify(window.primerApi.readRefusal(" + spec + "))")
        )

    with_words = refusal(
        '{status: 403, detail: "Workspace \'w-1\' needs full access",'
        " envelope: {extensions: {code: 'access_denied'}}}"
    )
    assert with_words["code"] == "access_denied"
    assert with_words["message"] == "Workspace 'w-1' needs full access"

    bare = refusal("{status: 403, envelope: {extensions: {code: 'access_denied'}}}")
    assert bare["message"] == "You don't have enough access for this."
