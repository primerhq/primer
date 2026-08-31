"""US-014 M1-M5: mobile shell entry, Inbox, the chat screen, Spaces+Files, More.

NV_Shell reads primerApi.useViewport() and swaps to NV_MobileShell
instead of the actbar/topbar/studio tree when isMobile; tablet and
?force-desktop=1 both keep the desktop chrome (useViewport's own
contract - see test_viewport_hook.py). NV_MobileShell itself is a
bottom nav (shared/mobile-tabs.jsx): Inbox (M2) is the cross-workspace
attention feed as cards via shared/card-list.jsx (CardList/Card).
Design ruling (2026-08-29): the feed is a TRIAGE surface, not a
squeezed decision screen - approval cards get an inline Approve button
(tool_call_id resolved lazily on press, zero extra fetches at render)
plus Review…; ask/parked cards get Review… only, whole-card tap doing
the same thing.

Review… (and Spaces' own session rows) land on NV_MobileChatScreen
(M3): a full-screen takeover mounting nv-session-doc.jsx's own
NV_SessionDoc verbatim, restyled - not forked - via CSS scoped under
.nv-mobile-chat: .nv-turn-user/.nv-turn-agent become left/right message
bubbles, .nv-trace-split/.nv-bind-menu/the "⋯" .nv-menu-right actions
dropdown all reposition to slide up from the bottom, and --hit (the
composer's sizing token) is bumped to 44px for the whole mobile shell.
Mid-run queueing and mic hold/double-tap-latch are reused as-is; the
mic also gets real touch handlers alongside its existing mouse ones.

Spaces (M4) is the workspace/session tree (expand -> sessions, pulse/
attention dots) + a Create Session FAB (a staged BottomSheet form:
workspace picker, agent/graph picker w/ search, name + first
instruction). Files (M4) is a workspace picker (implicit with one
workspace) + tree, READ-ONLY this phase; a file or a History -> commit
selection pushes FULL-SCREEN over the whole shell (nav bar included) via
con.setDoc + NV_MobileShell's own con.doc.kind dispatch - the same
shell-level takeover M3's chat screen uses. diff reuses nv-studio.jsx's
own NV_renderStudioDoc (pure display, nothing to strip); file does NOT
reuse the desktop NV_FileDoc editor (draft/save/etag-conflict UI) - a
small dedicated read-only NV_MobileFileView instead.

More (M5) is profile+theme (nv-chrome.jsx's own setTheme persistence,
not forked) + system health cards (NV_HealthCards, nv-system.jsx,
reused directly rather than re-fetched) + Platform sections as
read-only lists: NV_PLAT_GROUPS' own navs (nv-platform.jsx) minus
"providers" (no generic {list, card} entry to reuse), each nav's
entities via NV_PLAT_PAGES[nav].list/.card - the SAME "nv-plat:" + nav
cache key and {name, sub, chip, facts} view-model the desktop cards
use - opening ONE generic fact-sheet BottomSheet rather than 13 bespoke
ones. Triggers get a real Fire-now (fire_now, newly ported to sh-api.jsx
- the classic ui/components/triggers.jsx already had it); every other
kind's sheet just says "Edit on desktop" - no create/edit/delete on
mobile. A desktop-only overlay name arriving via con.overlay (a pasted
link, or a palette entity row) while mobile resolves to the same fact
sheet instead of NV_OverlayHost's full desktop page.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
MOBILE = (
    UI / "components" / "console" / "nv-mobile-shell.jsx"
).read_text(encoding="utf-8")
PLATFORM = (
    UI / "components" / "console" / "nv-platform.jsx"
).read_text(encoding="utf-8")
API = (UI / "components" / "shell" / "sh-api.jsx").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "styles.css").read_text(encoding="utf-8")


def test_mobile_shell_file_is_registered_in_index() -> None:
    assert "components/console/nv-mobile-shell.jsx" in HTML


def test_shell_reads_use_viewport_for_is_mobile() -> None:
    assert "window.primerApi.useViewport()" in SHELL
    assert "isMobile" in SHELL


def test_shell_swaps_to_mobile_shell_when_is_mobile() -> None:
    m = re.search(
        r'isMobile && typeof window\.NV_MobileShell === "function"'
        r'[\s\S]{0,80}',
        SHELL,
    )
    assert m
    assert "<window.NV_MobileShell />" in m.group(0)


def test_nv_root_stays_common_to_both_branches() -> None:
    """The swap picks the CHROME, not a second root - overlay/palette/
    confirm/toast hosts must stay mounted regardless of viewport
    (all state/resources shared with the desktop shell, per the phase
    plan), and nv-root must still be the one testid BDD's open_studio
    already waits on regardless of viewport band."""
    m = re.search(
        r'<div className="nv-root" data-testid="nv-root"[\s\S]{0,4000}',
        SHELL,
    )
    assert m
    body = m.group(0)
    assert "data-mobile={isMobile" in body
    assert "window.NV_ActivityBar" in body
    assert "window.NV_MobileShell" in body
    assert "window.NV_OverlayHost" in body
    assert "window.NV_Palette" in body
    assert "ConfirmHost" in body
    assert "NV_ToastHost" in body


def test_desktop_branch_is_a_fragment_not_a_second_root() -> None:
    # The desktop chrome must not introduce its own nv-root/testid wrapper -
    # exactly one nv-root per render, whichever branch is active.
    assert SHELL.count('data-testid="nv-root"') == 1


def test_mobile_shell_reuses_the_shared_mobile_tabs_component() -> None:
    assert "window.MobileTabs" in MOBILE
    assert 'data-testid="nv-mobile-shell"' in MOBILE


def test_mobile_shell_defines_the_four_bottom_nav_tabs() -> None:
    for tab_id in ('"inbox"', '"spaces"', '"files"', '"more"'):
        assert tab_id in MOBILE, tab_id


def test_inbox_tab_carries_a_real_attention_count() -> None:
    """Not a stub number - shares the rail's own aggregate + cache key
    (nv-rail.jsx) so switching viewport bands doesn't double-fetch."""
    assert '"nv-rail-inbox"' in MOBILE
    assert "SH_api.pendingAttention(signal)" in MOBILE
    assert "inboxItems.length" in MOBILE


def test_stub_panels_are_named_for_their_own_later_phase() -> None:
    # Inbox (M2) and Spaces/Files (M4) are real content now - their old
    # stub text is gone. More (M5) is a separate phase/owner - not
    # asserting on its current shape here beyond "it's not this file's
    # retired M2/M4 stub text."
    assert "The cross-workspace attention feed lands here" not in MOBILE
    assert (
        "The workspace/session tree and Create Session FAB land here"
        not in MOBILE
    )
    assert "The workspace file browser and history land here" not in MOBILE


# ---------------------------------------------------------------------------
# US-014 M2: the Inbox tab's real cards.
# ---------------------------------------------------------------------------


def test_inbox_panel_renders_cards_or_an_empty_state() -> None:
    assert "function NV_MobileInboxPanel(props)" in MOBILE
    assert '"nv-mobile-panel:inbox"' in MOBILE
    assert "Nothing needs you right now." in MOBILE


def test_inbox_cards_use_the_shared_card_list_primitives() -> None:
    """shared/card-list.jsx (CardList/Card) was already in-tree, unused
    by the console - reuse it rather than hand-rolled .card/.card-row
    markup, same "primitives already in-tree" principle as MobileTabs."""
    assert "window.CardList" in MOBILE
    assert "window.Card " in MOBILE or "window.Card\n" in MOBILE or "<window.Card" in MOBILE


def test_approve_button_lazily_resolves_tool_call_id_on_press() -> None:
    """No render-time per-item fetch (that was the rejected N+1 design) -
    NV_MobileApproveButton only calls sessionPendingYields inside its own
    click handler, then posts through the exact SH_api.approve the
    desktop decision card uses, so it is recorded identically."""
    m = re.search(r"function NV_MobileApproveButton\(props\)[\s\S]{0,1600}", MOBILE)
    assert m
    body = m.group(0)
    assert "function press(" in body
    assert "SH_api.sessionPendingYields(it.workspace_id, it.session_id)" in body
    assert "SH_api.approve(it.session_id, row.tool_call_id)" in body
    # Not a useResource hook - this must not run on every render/poll.
    assert "useResource" not in body


def test_only_approval_cards_get_the_inline_approve_button() -> None:
    m = re.search(r"function NV_MobileInboxCard\(props\)[\s\S]{0,2200}", MOBILE)
    assert m
    body = m.group(0)
    assert 'it.kind === "approval"' in body
    assert re.search(r"isApproval \?\s*\(?\s*\n?\s*<NV_MobileApproveButton", body)


def test_ask_and_parked_cards_get_no_inline_decision_ui() -> None:
    """Only Review (+ whole-card tap) - no answer textarea, no inline
    Reject-with-reason. Those stay in the full session (Review reaches
    it); the mobile Inbox is a triage feed, not a squeezed decision
    screen (design ruling, 2026-08-29)."""
    assert "NV_DecisionCard" not in MOBILE
    assert "NV_AskCard" not in MOBILE
    assert "nv-reject" not in MOBILE
    assert "nv-ask-answer" not in MOBILE


def test_non_approval_cards_get_whole_card_tap_to_review() -> None:
    m = re.search(r"<window\.Card[\s\S]{0,150}", MOBILE)
    assert m
    assert "onClick={isApproval ? undefined : review}" in m.group(0)


def test_review_affordance_present_on_every_card() -> None:
    assert 'data-testid={"nv-mobile-inbox-review:"' in MOBILE
    assert "Review…" in MOBILE


def test_review_routes_through_the_single_push_combined_navigation() -> None:
    """Same con.openInWorkspace + promoteDoc combo the rail/palette use
    (F2/F3, US-013) - not a bespoke mobile navigation path."""
    m = re.search(r"function review\(\)[\s\S]{0,300}", MOBILE)
    assert m
    body = m.group(0)
    assert "con.openInWorkspace(it.workspace_id" in body
    assert 'con.promoteDoc("session:" + it.session_id)' in body


def test_inbox_action_buttons_carry_the_touch_target_class() -> None:
    approve = re.search(r"function NV_MobileApproveButton\(props\)[\s\S]{0,1600}", MOBILE)
    assert approve and "touch-target" in approve.group(0)
    card = re.search(r"function NV_MobileInboxCard\(props\)[\s\S]{0,2200}", MOBILE)
    assert card and "touch-target" in card.group(0)


# ---------------------------------------------------------------------------
# US-014 M3: the mobile chat screen.
# ---------------------------------------------------------------------------


def test_chat_screen_mounts_the_real_session_doc() -> None:
    """Reuses NV_SessionDoc verbatim - the same mount nv-studio.jsx's own
    NV_renderStudioDoc uses on desktop - not a mobile reimplementation.

    RETARGET (SEV cross-session state bleed fix): NV_SessionDoc now
    mounts with `key={sid}` so switching which session's screen is
    shown forces a real unmount/remount instead of reusing the same
    component instance (and its stale useResource snapshot) across
    sessions."""
    m = re.search(r"function NV_MobileChatScreen\(props\)[\s\S]{0,1300}", MOBILE)
    assert m
    body = m.group(0)
    assert "<window.NV_SessionDoc key={sid} sid={sid}" in body
    assert 'data-testid="nv-mob-session-screen"' in body
    assert 'data-testid="nv-mob-screen-back"' in body
    assert "con.setDoc(null)" in body


def test_chat_screen_title_uses_the_resolved_session_name() -> None:
    m = re.search(r"function NV_MobileChatScreen\(props\)[\s\S]{0,900}", MOBILE)
    assert m
    assert "con.resolveSessionMeta" in m.group(0)


def test_chat_screen_dispatches_from_the_shell_for_session_docs() -> None:
    assert "con.doc.kind === \"session\"" in MOBILE
    assert "<NV_MobileChatScreen doc={con.doc} />" in MOBILE
    assert "NV_MobileDocFallback" not in MOBILE


def test_mic_gets_real_touch_handlers_alongside_the_existing_mouse_ones() -> None:
    """A synthesized mousedown/up from a touch press is unreliable on some
    mobile browsers - real touch handlers fix that without touching the
    latch state machine (micDown/micUp are called unchanged)."""
    doc = (
        Path(__file__).resolve().parents[2] / "ui" / "components" / "console"
        / "nv-session-doc.jsx"
    ).read_text(encoding="utf-8")
    m = re.search(r'data-testid="nv-mic"[\s\S]{0,1100}', doc)
    assert m
    body = m.group(0)
    assert "onMouseDown={micDown} onMouseUp={micUp}" in body
    assert "onTouchStart={function (ev) { ev.preventDefault(); micDown(); }}" in body
    assert "onTouchEnd={function (ev) { ev.preventDefault(); micUp(); }}" in body


def test_messenger_bubbles_are_css_only_scoped_to_the_mobile_chat() -> None:
    """.nv-turn-user/.nv-turn-agent already exist on every turn (nv-
    session-doc.jsx) with no distinguishing style - the mobile bubble
    layout is a scoped override, not a JS change to the shared
    transcript renderer."""
    assert ".nv-mobile-chat .nv-turn-user" in CSS
    assert ".nv-mobile-chat .nv-turn-agent" in CSS


def test_trace_and_menus_become_bottom_sheets_under_the_mobile_chat_scope() -> None:
    """.nv-trace-split, .nv-bind-menu and the session actions .nv-menu-
    right dropdown are all desktop side-panel/dropdown positioning by
    default - repositioned here, not forked, so nv-session-doc.jsx's own
    close affordances (nv-trace-close, a row click) keep working
    unchanged."""

    def _pinned_to_the_bottom(body: str) -> bool:
        return "bottom: 0" in body or "inset: auto 0 0 0" in body

    trace = re.search(r"\.nv-mobile-chat \.nv-trace-split \{([^}]*)\}", CSS)
    assert trace
    assert _pinned_to_the_bottom(trace.group(1))

    # .nv-bind-menu and .nv-menu-right share one comma-joined rule.
    menus = re.search(
        r"\.nv-mobile-chat \.nv-bind-menu,\s*"
        r"\.nv-mobile-chat \.nv-menu-right \{([^}]*)\}",
        CSS,
    )
    assert menus
    assert _pinned_to_the_bottom(menus.group(1))


def test_hit_token_bumped_to_44px_for_the_mobile_shell_composer() -> None:
    """--hit's own default (36px) and compact mode (32px) are both below
    the review's 44px mobile floor; nv-composer-iconbtn/nv-send-btn/
    nv-stop-btn are its only consumers (test_touch_targets.py's
    HIT_SIZED list), so bumping it for the whole mobile shell only
    touches the composer."""
    m = re.search(r"\.nv-mobile-shell\s*\{([^}]*)\}", CSS)
    assert m
    assert "--hit: 44px" in m.group(1)


def test_mobile_gets_the_compact_queue_label_desktop_keeps_queue() -> None:
    """The send button's queue BEHAVIOR (data-mode="queue", the desktop's
    own steer-while-running semantics) is unconditional and unchanged -
    queueLabel only overrides its TEXT. NV_MobileChatScreen passes "+Q"
    (the mockup's compact mobile treatment); every other caller (desktop's
    NV_renderStudioDoc) passes nothing and keeps "Queue" - one send
    button implementation, not a mobile fork."""
    doc = (
        Path(__file__).resolve().parents[2] / "ui" / "components" / "console"
        / "nv-session-doc.jsx"
    ).read_text(encoding="utf-8")
    assert 'data-mode={props.running ? "queue" : "send"}' in doc
    assert "props.running ? (props.queueLabel || \"Queue\") : \"Send\"" in doc
    assert "queueLabel={props.queueLabel}" in doc
    assert 'window.NV_SessionDoc key={sid} sid={sid} queueLabel="+Q"' in MOBILE


def test_single_implementation_guard_no_forked_session_doc_internals() -> None:
    """US-014 M3's whole architecture bet: NV_MobileChatScreen mounts
    NV_SessionDoc verbatim rather than forking any of its state - the
    live streaming tool-call fold (A4), per-session drafts, and
    optimistic sends must each still have exactly ONE implementation,
    and none of it duplicated into the mobile file."""
    doc = (
        Path(__file__).resolve().parents[2] / "ui" / "components" / "console"
        / "nv-session-doc.jsx"
    ).read_text(encoding="utf-8")
    # A4 live-part fold: exactly one push site in its one home.
    assert doc.count("liveToolParts.push(") == 1
    assert "window.parsePartialJson" in doc
    # Per-session drafts: one shared store, keyed by sid.
    assert doc.count("var NV_DRAFTS = {};") == 1
    # Optimistic sends: one state slot.
    assert doc.count("var optimisticState = React.useState(null);") == 1
    # None of it forked into the mobile file - NV_MobileChatScreen must
    # be a thin mount, not a second implementation.
    for marker in (
        "liveToolParts", "parsePartialJson", "NV_DRAFTS", "optimisticState",
    ):
        assert marker not in MOBILE, marker


def test_inbox_has_tap_driven_refetch_like_the_rail() -> None:
    assert "window.useWorkspaceTapListener(con.wid" in MOBILE
    for cls in ('"yielded"', '"resumed"', '"done"'):
        assert cls in MOBILE, cls


def test_nav_tabs_carry_the_touch_target_class() -> None:
    """shared/mobile-tabs.jsx's own tab buttons already render
    "mobile-tab touch-target" - .touch-target's floor is --tap-min (44px,
    styles.css:2225/2230-2236). Pinning the class name here catches a
    future MobileTabs edit that drops it silently."""
    tabs_src = (
        UI / "components" / "shared" / "mobile-tabs.jsx"
    ).read_text(encoding="utf-8")
    assert "touch-target" in tabs_src
    m = re.search(r"--tap-min:\s*(\d+)px", CSS)
    assert m and int(m.group(1)) >= 44


def test_bottom_nav_css_is_scoped_to_the_mobile_shell() -> None:
    """shared/mobile-tabs.jsx ships a TOP bar for the classic studio's
    mobile mode - the console wants a BOTTOM nav. The override must be
    scoped under .nv-mobile-shell, not a rewrite of the shared .mobile-
    tabs rule other consumers still rely on."""
    assert ".nv-mobile-shell .mobile-tabs {" in CSS
    m = re.search(r"\.nv-mobile-shell \.mobile-tabs \{([^}]*)\}", CSS)
    assert m
    assert "bottom: 0" in m.group(1)
    # The classic (unscoped) rule must still exist, untouched.
    assert re.search(r"(?<!-shell )\.mobile-tabs \{", CSS)


def test_bundle_transpiles_with_the_mobile_shell() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body


# ---------------------------------------------------------------------------
# US-014 M4: Spaces tab - workspace tree, pulse/attention dots, FAB.
# ---------------------------------------------------------------------------


def test_spaces_tree_reuses_the_rail_and_shell_level_workspace_cache() -> None:
    """All state/resources shared with the desktop shell (phase plan) -
    con.workspaces is nv-shell.jsx's own "nv-workspaces" resource (free,
    no extra fetch); sessions/attention share the rail's own cache keys
    (nv-rail.jsx) rather than a third independent poll for the same data."""
    m = re.search(r"function NV_MobileSpaces\(\)[\s\S]{0,1500}", MOBILE)
    assert m
    body = m.group(0)
    assert "con.workspaces" in body
    assert '"nv-rail-all-sessions"' in body
    assert '"nv-rail-inbox"' in body


def test_spaces_tree_expands_a_workspace_to_its_sessions() -> None:
    assert '"nv-mob-ws:"' in MOBILE
    assert '"nv-mob-session:"' in MOBILE
    assert "setExpanded" in MOBILE


def test_spaces_session_rows_carry_pulse_or_attention_dots() -> None:
    assert "nv-dot-attention" in MOBILE
    assert "<NV_Mobile_Pulse" in MOBILE
    # A LOCAL copy (hook-bearing leaf), not a cross-file reference to
    # nv-rail.jsx's own NV_Rail_SessionPulse - see nv-rail.jsx's own
    # comment on why that pattern is avoided in this codebase.
    assert "function NV_Mobile_Pulse(props)" in MOBILE


def test_spaces_session_open_matches_the_inbox_reviews_own_navigation() -> None:
    """Same con.openInWorkspace + promoteDoc combo the Inbox tab's own
    review() uses (M2) - both land on the identical NV_MobileChatScreen
    (M3) takeover, not two different mobile paths."""
    m = re.search(r"function openSession\(s, wid\)[\s\S]{0,400}", MOBILE)
    assert m
    body = m.group(0)
    assert "con.openInWorkspace(wid" in body
    assert 'con.promoteDoc("session:" + s.session_id)' in body


def test_spaces_fab_opens_the_create_session_sheet() -> None:
    assert "window.Fab" in MOBILE
    assert "<NV_MobileCreateSessionSheet" in MOBILE


def test_create_session_sheet_stages_workspace_then_bind_then_details() -> None:
    """Task brief: "workspace picker sheet -> agent/graph picker sheet
    with search -> name + first-instruction inputs" - three full-attention
    BottomSheet stages, not nv-overlays.jsx's own inline dropdown-style
    bind picker."""
    m = re.search(
        r"function NV_MobileCreateSessionSheet\(props\)[\s\S]{0,9000}", MOBILE
    )
    assert m
    body = m.group(0)
    for stage in ('"workspace"', '"bind"', '"details"'):
        assert stage in body, stage
    assert '"nv-mob-ns-wid:"' in body
    assert '"nv-mob-ns-bind:"' in body
    assert 'data-testid="nv-mob-ns-search"' in body
    assert 'data-testid="nv-mob-ns-name"' in body
    assert 'data-testid="nv-mob-ns-instr"' in body


def test_create_session_sheet_reuses_the_desktop_overlays_own_entity_cache() -> None:
    m = re.search(
        r"function NV_MobileCreateSessionSheet\(props\)[\s\S]{0,1600}", MOBILE
    )
    assert m
    body = m.group(0)
    assert '"nv-ov:agents"' in body
    assert '"nv-ov:graphs"' in body


def test_create_session_submit_body_matches_the_shared_new_session_contract() -> None:
    """auto_start follows whether an instruction was typed; a typed name
    is sent, omitting binding asks for the default agent - same
    SharedNewSessionForm contract nv-overlays.jsx's own
    NV_CreateSessionOverlay submits, deliberately WITHOUT that overlay's
    advanced/graph-input-schema section, which the brief does not ask
    for."""
    m = re.search(r"function submit\(\)[\s\S]{0,900}", MOBILE)
    assert m
    body = m.group(0)
    assert "auto_start: instr.trim().length > 0" in body
    assert "trimmedName" in body and "body.name = trimmedName" in body
    assert "SH_api.createSession(wid, body)" in body


def test_create_session_stamps_meta_from_the_form_not_the_response() -> None:
    """Same "stamp what we already know" pattern nv-overlays.jsx's own
    NV_CreateSessionOverlay uses (F1/F10, 2026-08-29 UI review) - the new
    tab's label/glyph do not wait on the response echoing name/binding
    back in the same shape."""
    m = re.search(r"function submit\(\)[\s\S]{0,1300}", MOBILE)
    assert m
    assert "props.onCreated(row || {}, wid, trimmedName, binding)" in m.group(0)
    m2 = re.search(r"onCreated=\{function \(row, wid, name, binding\)[\s\S]{0,700}", MOBILE)
    assert m2
    assert "con.stampSessionMeta(sid, { wid: wid, name: name, binding: binding })" in m2.group(0)


# ---------------------------------------------------------------------------
# US-014 M4: Files tab - workspace picker, tree, history, full-screen docs.
# ---------------------------------------------------------------------------


def test_files_tab_workspace_picker_uses_the_workspace_switch_verb() -> None:
    """Same verb-based switch nv-studio.jsx's own onSelectWorkspace uses
    (con.registry.get("workspace.switch").run({wid})) - not a bespoke
    mobile-only setWid path."""
    m = re.search(r"function pickWorkspace\(newWid\)[\s\S]{0,250}", MOBILE)
    assert m
    body = m.group(0)
    assert 'con.registry.get("workspace.switch")' in body
    assert "verb.run({ wid: newWid })" in body


def test_files_tree_shares_the_desktop_sidebars_own_cache_keys() -> None:
    """Same SH_api.keys.tree/log cache keys nv-files-sidebar.jsx's desktop
    panel uses - an expand here and on desktop share one fetch/poll."""
    m = re.search(r"function NV_MobileFiles\(\)[\s\S]{0,1500}", MOBILE)
    assert m
    body = m.group(0)
    assert "SH_api.keys.tree(" in body
    assert "SH_api.keys.log(" in body


def test_files_history_toggles_a_commit_list_that_opens_a_diff() -> None:
    assert '"nv-mob-files-history"' in MOBILE
    assert '"nv-mob-commit:"' in MOBILE
    assert 'con.setDoc({ kind: "diff", ref: c.sha })' in MOBILE


def test_file_row_opens_via_con_set_doc_not_local_state() -> None:
    """Opening a file goes through con.setDoc (the shared, URL-backed doc
    state every other doc-opening surface uses), not a Files-tab-only
    screen variable - NV_MobileShell's own con.doc.kind dispatch is what
    makes it full-screen (see the takeover test below)."""
    assert 'con.setDoc({ kind: "file", ref: entry.path })' in MOBILE


def test_file_and_diff_docs_take_over_the_whole_shell_like_the_session_fallback() -> None:
    m = re.search(r"function NV_MobileShell\(\)[\s\S]{0,4500}", MOBILE)
    assert m
    body = m.group(0)
    assert 'con.doc.kind === "file" || con.doc.kind === "diff"' in body
    assert "<NV_MobileFileScreen doc={con.doc} />" in body


def test_diff_screen_reuses_the_desktop_doc_dispatcher() -> None:
    """diff is pure display (NV_DiffDoc has no write actions) - reusing
    nv-studio.jsx's own NV_renderStudioDoc dispatcher for it carries over
    diff rendering for free."""
    m = re.search(r"function NV_MobileFileScreen\(props\)[\s\S]{0,700}", MOBILE)
    assert m
    body = m.group(0)
    assert 'doc.kind === "diff" ? NV_renderStudioDoc(con, doc)' in body


def test_file_screen_is_read_only_not_the_desktop_editor() -> None:
    """Read-only this phase - no upload/new/rename on mobile. NV_FileDoc
    is a full editor (draft state, Ctrl+S, etag/412-conflict UI) - reusing
    it for the mobile file screen would have quietly shipped Save on
    mobile, so this is its own small NV_MobileFileView instead."""
    m = re.search(r"function NV_MobileFileScreen\(props\)[\s\S]{0,700}", MOBILE)
    assert m
    assert "<NV_MobileFileView path={doc.ref} />" in m.group(0)

    view = re.search(r"function NV_MobileFileView\(props\)[\s\S]{0,900}", MOBILE)
    assert view
    body = view.group(0)
    assert "SH_api.fileRead(con.wid, path, signal)" in body
    # No editor affordances at all - draft state, save, Ctrl+S.
    assert "setDraft" not in body
    assert "save(" not in body
    assert "nv-file-save" not in body


def test_mobile_files_subtree_is_a_local_copy_not_a_cross_file_hook() -> None:
    assert "function NV_Mobile_FilesSubtree(props)" in MOBILE


def test_files_workspace_picker_is_implicit_with_one_workspace() -> None:
    """Task brief: "workspace picker sheet (only if >1 workspace, else
    implicit)" - a real <button> (opens the sheet) only when there is an
    actual choice; otherwise a plain, non-interactive label."""
    m = re.search(r"function NV_MobileFiles\(\)[\s\S]{0,5000}", MOBILE)
    assert m
    body = m.group(0)
    assert "workspaces.length > 1 ?" in body
    assert 'data-testid="nv-mob-files-ws-picker"' in body
    assert 'data-testid="nv-mob-files-ws-label"' in body


def test_fab_is_lifted_above_the_sticky_bottom_tab_bar() -> None:
    """shared/floating-action.jsx's own .fab sits at bottom: var(--mobile-
    pad-y) - the mobile shell's sticky bottom nav (M1 override) occupies
    that same strip, so an unscoped FAB would render hidden behind it."""
    assert ".nv-mobile-shell .fab {" in CSS
    m = re.search(r"\.nv-mobile-shell \.fab \{([^}]*)\}", CSS)
    assert m
    assert "--tap-min" in m.group(1)


# ---------------------------------------------------------------------------
# US-014 M5: the More tab - profile/theme, health cards, Platform sections.
# ---------------------------------------------------------------------------


def test_more_tab_composes_profile_health_and_platform() -> None:
    assert "function NV_MobileMore(props)" in MOBILE
    assert '"nv-mobile-panel:more"' in MOBILE
    m = re.search(r"function NV_MobileMore\(props\)[\s\S]{0,600}", MOBILE)
    assert m
    body = m.group(0)
    assert "<NV_MobileProfileTheme" in body
    assert "<NV_HealthCards" in body
    assert "<NV_MobilePlatform" in body


def test_theme_setter_reuses_nv_chromes_own_persistence_key() -> None:
    """nv-chrome.jsx's own NV_ProfileMenu.setTheme is a closure, not
    exported - this is a second call site for the SAME 4 steps
    (setTweak, the data-theme attribute, localStorage, con.bump()), not a
    forked scheme. NV_themeStorageKey (the actual storage key) is the one
    piece that MUST be the literal shared function, or the two surfaces
    would silently disagree on where the preference lives."""
    m = re.search(r"function NV_mobileSetTheme\(con, next\)[\s\S]{0,500}", MOBILE)
    assert m
    body = m.group(0)
    assert 'window.primerApi.setTweak("theme", next)' in body
    assert 'document.documentElement.setAttribute("data-theme", next)' in body
    assert "NV_themeStorageKey(con.username)" in body
    assert "con.bump()" in body


def test_theme_segment_renders_dark_and_light() -> None:
    assert 'data-testid="nv-mob-theme-seg"' in MOBILE
    m = re.search(r'data-testid="nv-mob-theme-seg"[\s\S]{0,400}', MOBILE)
    assert m
    body = m.group(0)
    assert 'NV_mobileSetTheme(con, "dark")' in body
    assert 'NV_mobileSetTheme(con, "light")' in body


def test_health_cards_reused_directly_not_refetched() -> None:
    """Trace nv-system.jsx's dashboard fetch, reuse its cache key - the
    simplest way to guarantee that is to call the component itself
    rather than re-implementing its three useResource calls here."""
    assert "<NV_HealthCards />" in MOBILE
    assert '"nv-sys:health"' not in MOBILE
    assert '"nv-sys:sessions-active"' not in MOBILE


def test_platform_sections_reuse_the_desktop_nav_groups_and_cache_key() -> None:
    m = re.search(r"function NV_MobilePlatform\(props\)[\s\S]{0,1600}", MOBILE)
    assert m
    body = m.group(0)
    assert "NV_PLAT_GROUPS.map" in body
    assert '"nv-plat:" + nav' in body
    assert "window.NV_PLAT_PAGES" in body
    assert "page.list(apiFetch, signal)" in body


def test_platform_sections_skip_providers_no_generic_page_entry() -> None:
    """NV_PLAT_GROUPS' "Intelligence" group lists "providers" (nv-platform.
    jsx), but NV_PLAT_PAGES has no "providers" key - that nav is a
    class-catalog page (ProviderCatalog), not the generic {list, card}
    shape every other nav uses. Confirm the filter is real (matches
    nv-platform.jsx's own key set) rather than an assumption."""
    assert "providers" not in re.findall(r"^  (\w+): \{", PLATFORM, re.M)
    m = re.search(r"function NV_MobilePlatform\(props\)[\s\S]{0,1700}", MOBILE)
    assert m
    assert "g.ids.filter(function (id) { return window.NV_PLAT_PAGES[id]; })" \
        in m.group(0)


def test_platform_row_opens_the_generic_fact_sheet() -> None:
    assert 'data-testid={"nv-mob-plat-row:" + row.id}' in MOBILE
    assert "setSheet({ kind: nav, cardVM: vm, row: row })" in MOBILE
    assert "<NV_MobileFactSheet" in MOBILE


def test_fact_sheet_is_generic_not_per_entity_kind() -> None:
    """One component reading {name, sub, chip, facts} - no per-kind
    branches for the body (the footer's triggers-vs-everything-else
    split is the one deliberate exception, tested separately below)."""
    m = re.search(r"function NV_MobileFactSheet\(props\)[\s\S]{0,1800}", MOBILE)
    assert m
    body = m.group(0)
    assert "cardVM.facts" in body
    assert "cardVM.sub" in body
    assert "cardVM.chip" in body
    for bespoke in ("agents", "graphs", "toolsets", "channels", "harnesses"):
        assert ('"' + bespoke + '"') not in body, bespoke


def test_fact_rows_filter_out_empty_facts_before_indexing() -> None:
    """Live-pass regression (2026-08-29): NV_fact(k, v) (nv-platform.jsx)
    returns null for an empty/missing value, and the desktop's own card
    renderer already .filter(Boolean)s that away (nv-platform.jsx line
    ~433) before mapping - this component originally mapped the raw
    array straight from cardVM.facts, which threw "Cannot read
    properties of null (reading '0')" on f[0] the first time any row
    had one empty fact (e.g. an agent with no model_profile_id yet).
    Caught by an actual live click during the M5 verification pass, not
    a static read - do not remove this filter."""
    m = re.search(r"function NV_MobileFactSheet\(props\)[\s\S]{0,1800}", MOBILE)
    assert m
    assert "(cardVM.facts || []).filter(Boolean).map(" in m.group(0)


def test_triggers_get_fire_now_everything_else_says_edit_on_desktop() -> None:
    m = re.search(r"function NV_MobileFactSheet\(props\)[\s\S]{0,1800}", MOBILE)
    assert m
    body = m.group(0)
    assert 'props.kind === "triggers"' in body
    assert "SH_api.fireTrigger(props.row.id)" in body
    assert 'data-testid="nv-mob-fact-fire"' in body
    assert 'data-testid="nv-mob-fact-edit-note"' in body
    assert "Edit on desktop" in body


def test_fire_now_hits_the_real_trigger_endpoint() -> None:
    """The classic ui/components/triggers.jsx already has this action -
    this is the first time it is ported to the fresh console's SH_api."""
    m = re.search(r"fireTrigger: function \(id\)[\s\S]{0,200}", API)
    assert m
    assert '"/triggers/" + encodeURIComponent(id) + "/fire_now"' in m.group(0)


def test_no_create_edit_delete_affordances_on_mobile_platform() -> None:
    """Read-only this phase - no create/edit/delete on mobile, do not
    port overlays. NV_MobilePlatform/NV_MobileFactSheet must never touch
    a DELETE/PUT/PATCH route or open a confirm/prompt dialog."""
    for fn in ("NV_MobilePlatform", "NV_MobileFactSheet"):
        m = re.search(r"function " + fn + r"\(props\)[\s\S]*?\nfunction ", MOBILE)
        body = m.group(0) if m else MOBILE[MOBILE.index("function " + fn):]
        assert '"DELETE"' not in body, fn
        assert '"PUT"' not in body, fn
        assert '"PATCH"' not in body, fn
        assert "confirmDialog" not in body, fn
        assert "promptDialog" not in body, fn


def test_url_mapping_intercepts_platform_overlays_to_the_fact_sheet() -> None:
    """spec pt 6: a desktop-only overlay name arriving via con.overlay
    (pasted link, palette entity row) while mobile opens the fact sheet
    instead of NV_OverlayHost's full desktop page. new-session/new-
    workspace are not NV_PLAT_PAGES keys, so they fall through
    untouched - M4's own FAB flow is what mobile actually uses to
    create a session."""
    m = re.search(r"function NV_MobileShell\(\)[\s\S]{0,2700}", MOBILE)
    assert m
    body = m.group(0)
    assert "con.overlay && con.overlay.name" in body
    assert "window.NV_PLAT_PAGES[name]" in body
    assert "setPendingFactSheet({ kind: name, id: con.overlay.id })" in body
    assert "con.closeOverlay()" in body
    assert 'setActiveTab("more")' in body


def test_pending_fact_sheet_reaches_the_more_tab() -> None:
    assert "pending={pendingFactSheet}" in MOBILE
    assert "<NV_MobileMore pending=" in MOBILE
