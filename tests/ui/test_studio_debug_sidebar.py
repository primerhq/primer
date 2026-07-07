"""Task 14 of docs/superpowers/plans/2026-07-05-studio-agents-interact.md —
the right sidebar (`StudioActivity` in `ui/components/studio-activity.jsx`)
collapses by default (still the GLOBAL debug tracker: Action Required across
ALL sessions + the reused WorkspaceTap feed), and the ACTIVE session's own
pending interaction now also renders INLINE in its stream
(`ui/components/studio-center.jsx`), reusing the exact respond/approval
endpoints the global list already hits so the two surfaces can never drift.

Static-source + transpile-build checks only (the `tests/ui` suite
convention — see test_studio_activity.py / test_studio_run_view_interactive.py
/ test_session_adapter.py), plus one MiniRacer eval of the one new pure
helper this task adds (`ST_yieldInvalidates`) so the load-bearing cache-key
list is exercised for real rather than only substring-matched.

No browser/live server: see test_studio_run_view_interactive.py's docstring
for why <Transcript>'s live EventSource/poll effects aren't exercised
end-to-end here (would need the heavier tests/ui_e2e Playwright harness).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
ACTIVITY = UI / "components" / "studio-activity.jsx"
CENTER = UI / "components" / "studio-center.jsx"
INDEX = UI / "index.html"
STYLES = UI / "styles.css"


def _activity_src() -> str:
    return ACTIVITY.read_text(encoding="utf-8")


def _center_src() -> str:
    return CENTER.read_text(encoding="utf-8")


def _fn_block(src: str, start_marker: str, end_marker: str) -> str:
    """Slice `src` from `start_marker` up to (not including) `end_marker`."""
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _studio_activity_fn_src() -> str:
    return _fn_block(_activity_src(), "function StudioActivity(", "window.StudioActivity = StudioActivity;")


def _action_required_fn_src() -> str:
    return _fn_block(_activity_src(), "function ActionRequired(", "function WorkspaceActivity(")


def _session_agent_panel_src() -> str:
    return _fn_block(_center_src(), "function SessionAgentPanel(", "function SessionGraphPanel(")


def _session_graph_panel_src() -> str:
    return _fn_block(_center_src(), "function SessionGraphPanel(", "function ST_SessionPanel(")


def _inline_yields_fn_src() -> str:
    # ST_InlineYields is defined AFTER SessionGraphPanel (not next to its
    # sibling pure helpers ST_isAutonomous/ST_sessionTranscriptRows) —
    # deliberately, so the JSX-free pure-helpers slice those two other test
    # files py_mini_racer-eval stays JSX-free. See the component's own
    # file-header comment in studio-center.jsx for the full rationale.
    return _fn_block(_center_src(), "function ST_InlineYields(", "function ST_SessionPanel(")


# ---------------------------------------------------------------------------
# Right sidebar starts collapsed by default (studio-activity.jsx).
# ---------------------------------------------------------------------------


def test_debug_sidebar_starts_collapsed_by_default() -> None:
    fn = _studio_activity_fn_src()
    assert "var [collapsed, setCollapsed] = React.useState(true);" in fn


def test_debug_sidebar_toggle_flips_collapsed_state() -> None:
    fn = _studio_activity_fn_src()
    assert 'data-testid="debug-sidebar-toggle"' in fn
    assert "onClick={toggle}" in fn
    assert "setCollapsed(function(c) { return !c; });" in fn
    # a11y: expanded state reflected for assistive tech.
    assert 'aria-expanded={collapsed ? "false" : "true"}' in fn


def test_debug_sidebar_body_hidden_while_collapsed() -> None:
    fn = _studio_activity_fn_src()
    assert 'data-testid="debug-sidebar-body"' in fn
    assert 'display: collapsed ? "none" : "flex"' in fn


def test_debug_sidebar_toggle_has_aria_controls_matching_body_id() -> None:
    # a11y: the toggle button announces which element it expands/collapses.
    # aria-controls must reference the ACTUAL id on the body wrapper, not
    # just a matching-looking string in isolation.
    fn = _studio_activity_fn_src()
    assert 'aria-controls="debug-sidebar-body"' in fn
    assert 'id="debug-sidebar-body"' in fn


def test_debug_sidebar_expands_to_action_required_and_workspace_tap() -> None:
    # The body wrapper (hidden while collapsed, per the test above) still
    # contains BOTH the global Action Required list and the reused
    # WorkspaceTap feed — expanding reveals exactly those two, nothing new.
    fn = _studio_activity_fn_src()
    body = _fn_block(fn, 'data-testid="debug-sidebar-body"', "</div>\n    </div>\n  );\n}")
    assert "<ActionRequired wid={wid} studio={studio} onCountChange={setPendingCount} />" in body
    assert "<WorkspaceActivity wid={wid} />" in body


def test_action_required_and_workspace_activity_stay_mounted_when_collapsed() -> None:
    # Collapsing must be a pure CSS/visual toggle, not an unmount — the
    # poll + tap-reconcile subscriptions inside ActionRequired/WorkspaceTap
    # need to stay warm so the collapsed rail's badge count is always live
    # and re-expanding doesn't show a stale/cold panel. Guard against a
    # regression to conditional-mount (`{!collapsed && <ActionRequired`).
    fn = _studio_activity_fn_src()
    assert "{!collapsed &&" not in fn
    assert "{collapsed ? null :" not in fn


def test_debug_sidebar_badge_shows_live_pending_count() -> None:
    fn = _studio_activity_fn_src()
    assert 'data-testid="debug-sidebar-badge"' in fn
    assert "pendingCount > 0" in fn
    assert "var [pendingCount, setPendingCount] = React.useState(0);" in fn


def test_action_required_reports_its_count_up_to_the_shell() -> None:
    ar = _action_required_fn_src()
    assert "function ActionRequired({ wid, studio, onCountChange })" in _activity_src()
    assert "if (typeof onCountChange === \"function\") onCountChange(count);" in ar


# ---------------------------------------------------------------------------
# Regression guard: existing global Action Required testids/endpoints
# (test_studio_activity.py) must be untouched by this task's refactor.
# ---------------------------------------------------------------------------


def test_existing_action_required_surface_untouched() -> None:
    src = _activity_src()
    for testid in (
        "action-required",
        "action-required-list",
        "action-required-count",
        "action-item",
        "action-session-link",
        "approve",
        "reject",
        "respond",
        "cancel-yield",
        "workspace-activity",
        "studio-activity-root",
    ):
        assert f'data-testid="{testid}"' in src, f"Missing data-testid: {testid}"
    assert "tool_approval/respond" in src
    assert "ask_user/respond" in src
    assert "yields/pending" in src


# ---------------------------------------------------------------------------
# Inline session-yield affordances (studio-center.jsx) — the session-scoped
# counterpart to the global list above.
# ---------------------------------------------------------------------------


def test_inline_yields_component_exists() -> None:
    src = _center_src()
    assert "function ST_yieldInvalidates(" in src
    assert "function ST_InlineYields({ wid, sid, pending, messages, pushToast })" in src


def test_inline_yields_renders_nothing_without_a_pending_item() -> None:
    fn = _inline_yields_fn_src()
    assert "if (!wid || !sid || !pending || !pending.length) return null;" in fn


def test_inline_yields_mounted_in_both_agent_and_graph_panels() -> None:
    src = _center_src()
    assert src.count("<ST_InlineYields wid={wid} sid={sid} pending={conv.pending} messages={conv.messages} pushToast={pushToast} />") == 2

    agent = _session_agent_panel_src()
    graph = _session_graph_panel_src()
    for panel in (agent, graph):
        assert "<ST_InlineYields" in panel
        # Positioned after <Transcript>, before the Composer footer — mirrors
        # chat-refactor's CT_ApprovalGate ("right after the message list").
        transcript_idx = panel.index("<window.Transcript")
        inline_idx = panel.index("<ST_InlineYields")
        composer_idx = panel.index("<window.Composer")
        assert transcript_idx < inline_idx < composer_idx


def test_inline_yields_backed_by_session_scoped_pending_from_the_adapter() -> None:
    # conv.pending is session-adapter.jsx's own GET .../sessions/{sid}/
    # yields/pending resource (Task 10/11) — no second fetch is introduced
    # here, it's threaded straight through from SA_useSessionConversation.
    agent = _session_agent_panel_src()
    assert "var conv = window.SA_useSessionConversation({ sid: sid, wid: wid });" in agent
    assert "pending={conv.pending}" in agent


def test_inline_yields_hits_the_same_endpoints_as_the_global_list() -> None:
    fn = _inline_yields_fn_src()
    assert "tool_approval/respond" in fn
    assert 'decision: "approved"' in fn
    assert 'decision: "rejected"' in fn
    assert "ask_user/respond" in fn
    assert "/yields/" in fn and "/cancel" in fn


def test_inline_yields_testids_present() -> None:
    fn = _inline_yields_fn_src()
    for testid in (
        "session-inline-yields",
        "session-yield-item",
        "session-yield-approve",
        "session-yield-deny",
        "session-yield-respond",
        "session-yield-cancel",
    ):
        assert f'data-testid="{testid}"' in fn


def test_inline_yields_testids_are_distinct_from_global_action_required() -> None:
    # The global sidebar's items may render on-screen at the same time as
    # the active session's inline affordance (same underlying yield) — the
    # testids must never collide so each surface stays independently
    # locatable in a real DOM.
    fn = _inline_yields_fn_src()
    for global_testid in ("approve", "reject", "respond", "cancel-yield", "action-item"):
        assert f'data-testid="{global_testid}"' not in fn


# ---------------------------------------------------------------------------
# Global <-> inline sync: both invalidate the SAME two caches on every
# write, and reconcile off the session's own already-open tap tail (no
# second EventSource) rather than a fresh poll-only guess.
# ---------------------------------------------------------------------------


def test_inline_yields_invalidates_both_global_and_session_scoped_caches() -> None:
    fn = _inline_yields_fn_src()
    assert 'return ["session-adapter:pending:" + sid, "studio-yields-pending:" + wid];' in _center_src()
    # All four write paths (approve/reject/respond/cancel) share the same
    # invalidates list — none of them special-cases only one cache.
    assert fn.count("invalidates: invalidates") == 4


def test_inline_yields_reconciles_via_the_adapters_tap_tail_not_a_second_stream() -> None:
    fn = _inline_yields_fn_src()
    assert "new EventSource" not in fn, "must reuse session-adapter.jsx's existing tap, not open a second one"
    assert 'last.kind !== "yielded" && last.kind !== "resumed"' in fn
    assert "resourceApi.findKeys(baseKey).forEach(function (key) { resourceApi.refetchKey(key); });" in fn


# ---------------------------------------------------------------------------
# The other half of the sync: GLOBAL Action Required (studio-activity.jsx)
# responding to a yield must invalidate the session-scoped cache immediately
# too, so the INLINE panel (above) doesn't wait out its 4s poll to notice the
# yield the global sidebar just cleared.
# ---------------------------------------------------------------------------


def test_action_required_defines_a_session_pending_invalidation_helper() -> None:
    src = _activity_src()
    assert "function SA_invalidateSessionPending(sessionId)" in src
    # Same findKeys/refetchKey primitive ST_yieldInvalidates' useMutation
    # callers use (studio-center.jsx), not a bespoke mechanism.
    helper = _fn_block(src, "function SA_invalidateSessionPending(", "function ActionRequired(")
    assert "window.primerApi && window.primerApi._resource" in helper
    assert "resourceApi.findKeys(baseKey).forEach(function(key) { resourceApi.refetchKey(key); });" in helper


def test_all_four_global_yield_handlers_invalidate_session_scoped_pending() -> None:
    # approve / reject / respond / cancel — none of them special-cases only
    # the workspace-wide cache; every write path that clears a yield also
    # nudges the session-scoped one the inline panel reads.
    fn = _action_required_fn_src()
    assert fn.count("SA_invalidateSessionPending(item.session_id);") == 4


def test_sa_invalidate_session_pending_uses_the_exact_key_sa_useSessionConversation_registers() -> None:
    # Load-bearing string match: SA_useSessionConversation (session-adapter.jsx)
    # registers its pending resource under "session-adapter:pending:" + sid
    # with useResource's own deps-suffix composition (":: " + JSON deps).
    # findKeys() only matches on an EXACT key or a "<base>::"-prefixed key, so
    # SA_invalidateSessionPending must pass that literal base string — a
    # mismatched key (e.g. missing/extra text) would silently no-op.
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(
        """
        var window = {
          primerApi: {
            _resource: {
              findKeysCalls: [],
              refetchCalls: [],
              findKeys: function(base) {
                window.primerApi._resource.findKeysCalls.push(base);
                return [base + '::["sess1","ws1"]'];
              },
              refetchKey: function(key) {
                window.primerApi._resource.refetchCalls.push(key);
              }
            }
          }
        };
        """
    )
    fn_src = _fn_block(_activity_src(), "function SA_invalidateSessionPending(", "function ActionRequired(")
    ctx.eval(fn_src)
    ctx.eval('SA_invalidateSessionPending("sess1");')
    assert ctx.eval("window.primerApi._resource.findKeysCalls[0]") == "session-adapter:pending:sess1"
    assert ctx.eval("window.primerApi._resource.refetchCalls[0]") == 'session-adapter:pending:sess1::["sess1","ws1"]'


def test_sa_invalidate_session_pending_is_a_no_op_without_resource_api_or_session_id() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    fn_src = _fn_block(_activity_src(), "function SA_invalidateSessionPending(", "function ActionRequired(")
    ctx.eval(fn_src)
    # Must not throw when primerApi/_resource is missing (no window.primerApi
    # configured yet) or sessionId is falsy.
    ctx.eval('SA_invalidateSessionPending("sess1");')
    ctx.eval('SA_invalidateSessionPending(null);')


# ---------------------------------------------------------------------------
# Pure helper: ST_yieldInvalidates — exercised for real via MiniRacer
# (mirrors ST_isAutonomous in test_studio_run_view_interactive.py).
# ---------------------------------------------------------------------------


def test_st_yield_invalidates_pure_helper_via_mini_racer() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    fn_src = _fn_block(_center_src(), "function ST_yieldInvalidates(", "function ST_InlineYields(")
    ctx.eval(fn_src)
    ctx.eval('var out = ST_yieldInvalidates("ws1", "sess1");')
    assert ctx.eval("out.length") == 2
    assert ctx.eval("out[0]") == "session-adapter:pending:sess1"
    assert ctx.eval("out[1]") == "studio-yields-pending:ws1"


# ---------------------------------------------------------------------------
# studio-ux fix 1: collapsed = an actual thin rail (~40px), not just an
# internal display:none — the OUTER .st-col-right grid TRACK must shrink too
# so the center column reflows wider. See ui/styles.css's :has() rule next to
# .st-col-right and the collapsed-branching inline style on the toggle
# button below (both scoped desktop-only; the mobile off-canvas drawer is
# untouched — see test_studio_mobile_panel_toggles_and_drawers).
# ---------------------------------------------------------------------------


def _styles_src() -> str:
    return STYLES.read_text(encoding="utf-8")


def test_collapsed_rail_hides_bell_and_label_but_keeps_chevron_and_badge() -> None:
    fn = _studio_activity_fn_src()
    assert "var expanded = !collapsed;" in fn
    # Chevron (the toggle indicator) and the badge are unconditional; the bell
    # icon + the HORIZONTAL "Debug" text retract when collapsed (the collapsed
    # rail instead shows a compact VERTICAL Debug label — see the rail-label
    # test below).
    assert '{expanded && <Icon name="bell" size={13} style={{ flexShrink: 0 }} />}' in fn
    assert '{expanded && <span style={{ flex: 1, textAlign: "left" }}>Debug</span>}' in fn
    assert 'Icon name={collapsed ? "chevron-left" : "chevron-right"}' in fn
    assert '{pendingCount > 0 && (' in fn


def test_collapsed_toggle_button_restyles_into_a_narrow_column() -> None:
    fn = _studio_activity_fn_src()
    assert 'flexDirection: collapsed ? "column" : "row"' in fn
    assert 'height: collapsed ? "auto" : 34' in fn
    assert 'padding: collapsed ? "10px 4px" : "0 12px"' in fn


def test_collapsed_rail_shows_a_vertical_debug_label_as_the_expand_handle() -> None:
    # Discoverability: collapsed, the rail is a thin strip. A vertical "Debug"
    # label (writing-mode) makes it legibly the expand handle instead of a bare,
    # easily-missed chevron — operators kept asking how to re-open the panel.
    fn = _studio_activity_fn_src()
    assert "{collapsed && (" in fn
    assert 'data-testid="debug-sidebar-rail-label"' in fn
    assert 'writingMode: "vertical-rl"' in fn


def test_st_body_grid_shrinks_the_right_track_when_the_rail_is_collapsed() -> None:
    css = _styles_src()
    assert ".st-body:has(.studio-activity-root.is-collapsed)" in css
    assert (
        "grid-template-columns: var(--st-left-w, 248px) 5px minmax(0, 1fr) 5px 40px;"
        in css
    )
    assert '.studio-activity-root.is-collapsed { width: 40px; min-width: 40px; }' in css


def test_activity_root_actually_carries_the_class_the_collapse_css_targets() -> None:
    # THE linchpin (regression that shipped): the collapse CSS above keys off the
    # `studio-activity-root` CLASS. For a long time `studio-activity-root` was
    # ONLY a data-testid and the root's className was `{collapsed ? "is-collapsed"
    # : ""}` — so `.studio-activity-root.is-collapsed` matched NO element and the
    # rail silently never collapsed even though the CSS strings above all existed.
    # The root MUST apply the class as a real className (and still toggle
    # is-collapsed) so the :has() + width selectors engage.
    fn = _studio_activity_fn_src()
    assert (
        'className={"studio-activity-root" + (collapsed ? " is-collapsed" : "")}'
        in fn
    )


def test_collapsed_rail_grid_override_hides_the_right_resize_handle() -> None:
    css = _styles_src()
    assert '[data-testid="studio-resize-right"]' in css


def test_collapsed_rail_grid_override_is_desktop_only() -> None:
    # The mobile breakpoint renders .st-col-right as an off-canvas fixed
    # drawer (see the max-width:639px block) — this override's higher
    # specificity must not leak into that layout, so it's gated behind a
    # min-width media query rather than left unscoped.
    css = _styles_src()
    idx = css.index(".st-body:has(.studio-activity-root.is-collapsed)")
    preceding = css[:idx]
    media_idx = preceding.rfind("@media (min-width: 640px)")
    assert media_idx != -1, "the collapsed-rail grid override must sit inside a min-width media query"
    # No closing brace for that media block between its opening and our rule
    # (i.e. the rule is actually INSIDE it, not just preceded by it earlier
    # in the file).
    between = css[media_idx:idx]
    assert between.count("{") > between.count("}")


def test_action_required_and_workspace_activity_still_stay_mounted_when_collapsed() -> None:
    # Regression guard alongside the existing mount-guard test above: the
    # new `expanded` flag must not have been used to sneak a conditional
    # mount back in for the two subscription-owning children.
    fn = _studio_activity_fn_src()
    assert "{expanded && <ActionRequired" not in fn
    assert "{expanded && <WorkspaceActivity" not in fn


# ---------------------------------------------------------------------------
# Bundle transpile gate (whole bundle must still parse cleanly).
# ---------------------------------------------------------------------------


def test_bundle_transpiles_with_debug_sidebar_and_inline_yields() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/studio-activity.jsx === */" in text
    assert "/* === components/studio-center.jsx === */" in text
