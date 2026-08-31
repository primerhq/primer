"""US-007 R2 phase 2, cut over permanently in US-011a: the rail + tab
groups as the only Studio shell.

Design: docs/superpowers/uiv2/06-r2-phase2-design.md (local, gitignored).
TG model lives as nv-shell.jsx state; doc is a derived memo off it
instead of a second source of truth. con.setDoc/con.promoteDoc keep
their exact pre-existing signatures so out-of-scope callers
(nv-overlays.jsx, nv-files-sidebar.jsx, nv-file-docs.jsx) need no
changes. The phase-2 rollback flag (NV_TG_ENABLED) and its fallback
mount (nv-doc-host.jsx, nv-sessions-sidebar.jsx) were retired once the
cutover proved out - this is just how the shell works now.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
STUDIO = (UI / "components" / "console" / "nv-studio.jsx").read_text(encoding="utf-8")


def test_doc_is_derived_not_a_second_source_of_truth() -> None:
    assert "TG_activeDoc(tgModel)" in SHELL
    # The old standalone useState pair must be gone, not just unused.
    assert "var docState = React.useState(initial.doc);" not in SHELL
    assert "var setDoc = docState[1];" not in SHELL


def test_tg_model_seeds_from_the_initial_url_as_preview() -> None:
    m = re.search(r"var tgModelState = React\.useState\(function \(\) \{([\s\S]{0,300})", SHELL)
    assert m
    body = m.group(1)
    assert "TG_init()" in body
    assert "TG_openTab(base, initial.doc, {})" in body


def test_read_side_sync_opens_as_preview_and_leaves_null_alone() -> None:
    m = re.search(r"function onNav\(\)([\s\S]{0,700}?)\n    \}", SHELL)
    assert m
    body = m.group(1)
    assert "if (parsed.doc)" in body
    assert "TG_openTab(m, parsed.doc, {})" in body
    # No unconditional setDoc(parsed.doc) - a null doc must not be forced.
    assert "setDoc(parsed.doc)" not in body


def test_workspace_switch_no_longer_touches_doc_or_anchor() -> None:
    # Window widened past 500 (2026-08-29 UI review, F6): the chord/
    # surfaces explanatory comment now sits ahead of the executable body.
    m = re.search(r'id: "workspace\.switch"[\s\S]{0,900}', SHELL)
    assert m
    body = m.group(0)
    # Strip line comments first - the explanatory comment names the old
    # calls in prose, and only the executable form must actually be gone.
    code_only = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//")
    )
    assert "setDoc(null)" not in code_only
    assert "setAnchor(null)" not in code_only
    assert "setWid(arg.wid)" in code_only


def test_set_doc_null_closes_the_active_tab_instead_of_crashing() -> None:
    """Real bug caught in this round's own review: nv-doc-host.jsx's
    close() calls con.setDoc(null) when its last tab closes, and
    nv-session-doc.jsx's onDeleted calls con.setDoc(null) on session
    delete - the latter fires in the DEFAULT (tgEnabled) path too.
    TG_openTab(m, null, ...) would throw reading null.kind."""
    m = re.search(r"setDoc: function \(d\) \{([\s\S]{0,800}?)\n      \},", SHELL)
    assert m
    body = m.group(1)
    assert "if (!d)" in body
    assert "TG_activeDoc(m)" in body
    assert "TG_closeTab(m, active.id)" in body


def test_ctx_exposes_the_tg_surface_with_preserved_setdoc_signature() -> None:
    for needle in (
        "tgModel: tgModel",
        # F1 (2026-08-29 UI review): generalized wid-only resolveSessionWid
        # into resolveSessionMeta (wid + name + binding, for the tab label
        # and identity glyph, not just the pulse dot).
        "resolveSessionMeta: resolveSessionMeta",
        "onTgModelChange: function (next, op)",
        "promoteDoc: function (id)",
    ):
        assert needle in SHELL, needle
    # setDoc keeps its old (d) => void shape - every out-of-scope caller
    # (nv-overlays.jsx, nv-files-sidebar.jsx, nv-file-docs.jsx) calls it
    # unchanged.
    assert re.search(r"setDoc: function \(d\) \{", SHELL)


def test_on_tg_model_change_only_pushes_for_open() -> None:
    m = re.search(r'onTgModelChange: function \(next, op\) \{([\s\S]{0,150})', SHELL)
    assert m
    body = m.group(1)
    assert 'if (op === "open") markPush();' in body


def test_resolve_session_wid_reuses_the_rails_own_cache_key() -> None:
    assert '"nv-rail-all-sessions"' in SHELL


def test_studio_mounts_rail_and_tab_groups_unconditionally() -> None:
    assert "<window.NV_Rail" in STUDIO
    assert "<window.NV_TabGroups" in STUDIO
    # The pre-R2 fallback is gone, not just unreachable.
    assert "NV_DocHost" not in STUDIO
    assert "NV_SessionsSidebar" not in STUDIO


def test_rail_props_are_wired_to_console_verbs() -> None:
    m = re.search(r"<window\.NV_Rail[\s\S]{0,2000}?\n {8}/>", STUDIO)
    assert m
    body = m.group(0)
    for prop in (
        "selectedWorkspaceId={con.wid}",
        "onSelectWorkspace=",
        "onOpenSession=",
        # F2 follow-up (2026-08-29 UI review): the plain onCreateSession
        # prop retired with US-012b item 4's Inbox "+" - the workspace
        # context menu's "New session" is the only caller left, and it
        # needs a target wid, so nv-studio.jsx wires the combined
        # switch-and-open (con.createSessionInWorkspace) instead.
        "onCreateSessionInWorkspace=",
        "onCreateWorkspace=",
    ):
        assert prop in body, prop
    assert 'con.registry.get("workspace.switch")' in body
    # session.create no longer runs through the registry from here - the
    # workspace context menu needs a combined switch-and-open (F2 follow-
    # up), so onCreateSessionInWorkspace calls con.createSessionInWorkspace
    # directly instead.
    assert "con.createSessionInWorkspace" in body
    assert 'con.registry.get("workspace.create")' in body


def test_rail_session_open_uses_the_existing_two_step_promote_pattern() -> None:
    # F2 (2026-08-29 UI review): the cross-workspace case now routes
    # through con.openInWorkspace (one history entry) instead of a
    # separate workspace.switch + setDoc; the same-workspace case still
    # uses the plain setDoc + promoteDoc two-step. Window widened past
    # 400 for the added explanatory comment.
    m = re.search(r"onOpenSession=\{function \(session, wid\) \{([\s\S]{0,700})", STUDIO)
    assert m
    body = m.group(1)
    assert "con.openInWorkspace(wid, " in body
    assert 'con.setDoc({ kind: "session", ref: session.session_id })' in body
    assert 'con.promoteDoc("session:" + session.session_id)' in body


def test_tab_groups_props_are_wired() -> None:
    m = re.search(r"<window\.NV_TabGroups[\s\S]{0,300}?/>", STUDIO)
    assert m
    body = m.group(0)
    assert "model={con.tgModel}" in body
    assert "onModelChange={con.onTgModelChange}" in body
    assert "renderDoc={renderDoc}" in body
    # F1 (2026-08-29 UI review): resolveSessionWid generalized into
    # resolveSessionMeta (wid + name + binding).
    assert "resolveSessionMeta={con.resolveSessionMeta}" in body


def test_render_doc_dispatch_and_empty_state_survive_with_their_testids() -> None:
    for kind_check in (
        'tab.kind === "session"', "window.NV_SessionDoc",
        'tab.kind === "file"', "window.NV_FileDoc",
        'tab.kind === "diff"', "window.NV_DiffDoc",
        'tab.kind === "wiki"', "window.NV_WikiDoc",
    ):
        assert kind_check in STUDIO, kind_check
    assert 'data-testid="nv-center-empty"' in STUDIO
    assert 'data-testid="nv-empty-new-session"' in STUDIO


def test_files_sidebar_is_always_visible() -> None:
    # US-011a (notes 2.5): Files is its own always-visible right-side
    # panel now, not a Sessions|Files rail-toggle tab.
    assert "nv-rail-tab-files" not in STUDIO
    assert "window.NV_FilesSidebar" in STUDIO


def test_old_files_are_gone_not_just_unscripted() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert "components/console/nv-doc-host.jsx" not in html
    assert "components/console/nv-sessions-sidebar.jsx" not in html
    assert "components/console/nv-tab-groups.jsx" in html
    assert "components/console/nv-rail.jsx" in html
    assert not (UI / "components" / "console" / "nv-doc-host.jsx").exists()
    assert not (UI / "components" / "console" / "nv-sessions-sidebar.jsx").exists()


# ---------------------------------------------------------------------------
# F1 (2026-08-29 UI review): resolveSessionWid generalized into
# resolveSessionMeta(sid) -> {wid, name, binding}; stampSessionWid into
# stampSessionMeta so a freshly-opened/created session's tab shows its real
# label and glyph immediately, not just its pulse.
# ---------------------------------------------------------------------------


def test_session_meta_poll_map_carries_wid_name_and_binding() -> None:
    m = re.search(
        r"var sessionMetaById = React\.useMemo\(function \(\) \{([\s\S]{0,300})",
        SHELL,
    )
    assert m
    body = m.group(1)
    assert "wid: s.workspace_id" in body
    assert "name: s.name" in body
    assert "binding: s.binding" in body


def test_resolve_session_meta_prefers_the_poll_over_the_stamp() -> None:
    m = re.search(
        r"var resolveSessionMeta = React\.useCallback\(function \(sid\) \{"
        r"([\s\S]{0,120})",
        SHELL,
    )
    assert m
    assert "sessionMetaById[sid] || stampedMetaById[sid]" in m.group(1)


def test_stamp_session_meta_carries_the_full_shape_and_is_exposed() -> None:
    assert (
        "var stampSessionMeta = React.useCallback(function (sid, meta)"
        in SHELL
    )
    assert "stampSessionMeta: stampSessionMeta" in SHELL
    # The old wid-only seam must be gone, not left dangling alongside it.
    assert "stampSessionWid" not in SHELL
    assert "resolveSessionWid" not in SHELL


def test_rail_open_and_create_overlay_stamp_the_full_meta() -> None:
    assert "con.stampSessionMeta(session.session_id, {" in STUDIO
    assert "name: session.name, binding: session.binding" in STUDIO
    overlays = (
        UI / "components" / "console" / "nv-overlays.jsx"
    ).read_text(encoding="utf-8")
    m = re.search(r"con\.stampSessionMeta\(sid, \{([\s\S]{0,80})", overlays)
    assert m
    assert "name: trimmedName, binding: binding" in m.group(1)


def test_bundle_transpiles_with_the_integration() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
