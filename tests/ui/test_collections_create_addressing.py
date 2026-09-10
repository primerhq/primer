"""u0025 flake: creating a collection must address its detail overlay
without waiting on a full list refetch to resolve.

ROOT CAUSE, round 2 (01a08b77 - the round-1 fix, c6b9f8c5, recurred on
CI). Round 1 correctly diagnosed the race (useResource's reloadKey bump
mints a brand-new cache entry, so `rows` collapses to [] for the length
of the GET /collections round trip) but put the fallback (`justCreatedRow`)
in React state LOCAL to CollectionsPage. That state cannot survive the
transition it exists to cover: nv-overlays.jsx's NV_LegacyOverlay renders
CollectionsPage from two STRUCTURALLY DIFFERENT branches depending on
whether overlay.id is set (`bypassChrome` - the list view uses the shared
wide-sheet chrome, the detail view renders its own complete Modal so two
title bars/close buttons don't stack). Proven live via MiniRacer (not
inference - see test_bypasschrome_transition_forces_a_react_remount
below): the ROOT element type NV_LegacyOverlay returns differs between
the two branches (NV_OverlayPanel vs a bare div), so React cannot
reconcile the old fiber into the new one and remounts CollectionsPage
from scratch at EXACTLY the moment onCreate addresses into the
just-created row - the local-state fallback is destroyed the instant it
is needed, on a call it never gets to make.

The fix (round 2): KN_justCreatedCache is a plain module-scope object,
outside any component's lifecycle, so it survives the remount. Cleaned
up once the list catches up (effect) or on navigating back (onBack), so
a long session creating many collections does not accumulate entries.

Two verification styles:
* Static-source checks (matching the rest of the ui/ suite) pin the
  fallback's shape.
* Two MiniRacer-EXECUTED tests (real Babel transpile via
  primer.api._jsx_bundle.JSXBundler, not jsdom - this toolchain has none)
  prove the actual mechanism rather than re-describing it: one pins the
  remount fact itself (so it fails if a future refactor removes the
  structural branch this fix depends on being module-scoped for), the
  other proves the fix's own addressed-lookup logic survives a simulated
  remount (a fresh read against the SAME module-scope cache).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "ui" / "components" / "knowledge.jsx"
OVERLAYS_SRC = ROOT / "ui" / "components" / "console" / "nv-overlays.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _collections_page_body() -> str:
    src = _src()
    start = src.index("function CollectionsPage(")
    end = src.index("\nwindow.CollectionsPage = CollectionsPage;")
    return src[start:end]


# ---------------------------------------------------------------------------
# Static-source checks
# ---------------------------------------------------------------------------


def test_just_created_cache_is_module_scope_not_component_state() -> None:
    src = _src()
    # Must be declared BEFORE CollectionsPage (module scope), not inside
    # it (which is exactly the round-1 shape that didn't survive the
    # remount) and must not be React state.
    assert src.index("const KN_justCreatedCache = {};") < src.index(
        "function CollectionsPage("
    ), "KN_justCreatedCache must be declared at module scope, before CollectionsPage"
    body = _collections_page_body()
    assert "justCreatedRow" not in body, (
        "the round-1 component-local state must be fully removed, not left "
        "alongside the module-scope cache"
    )


def test_addressed_falls_back_to_the_just_created_cache() -> None:
    """The list-derived lookup must be tried FIRST (so steady-state
    behavior, once the refetch lands, is unchanged) with the
    module-scope cache only as a fallback for the race window."""
    body = _collections_page_body()
    addressed_block = body.split("const addressed =")[1].split(";\n")[0]
    assert "fromList" in addressed_block
    assert "KN_justCreatedCache[selectedId]" in addressed_block
    assert addressed_block.index("fromList") < addressed_block.index(
        "KN_justCreatedCache"
    ), "the list lookup must be tried before the just-created-cache fallback"


def test_on_create_stamps_the_cache_before_the_reload_bump() -> None:
    """Order matters only in that both must land before setSelected
    triggers the addressed render - assert both calls exist in the
    onCreate callback, not a stale variant that dropped one."""
    body = _collections_page_body()
    on_create = body.split("onCreate={(row) => {")[1].split("}}")[0]
    assert "KN_justCreatedCache[row.id] = row;" in on_create
    assert "setReloadKey((k) => k + 1);" in on_create
    assert "setSelected(row);" in on_create


def test_on_back_clears_the_just_created_cache_entry() -> None:
    """Hygiene: a stale cache entry must not linger past navigating back
    to the list."""
    body = _collections_page_body()
    on_back = body.split("onBack={() => {")[1].split("}}")[0]
    assert "delete KN_justCreatedCache[selected.id];" in on_back
    assert "setSelected(null);" in on_back


def test_cache_entry_is_cleaned_up_once_the_list_catches_up() -> None:
    body = _collections_page_body()
    assert "delete KN_justCreatedCache[selectedId];" in body
    # Must run in an effect (a render-body mutation is a React anti-pattern,
    # even though idempotent here) - assert it's inside a useEffect call
    # whose deps include fromList and selectedId.
    effect_start = body.index("React.useEffect(() => {\n    if (fromList")
    effect_block = body[effect_start:effect_start + 300]
    assert "[fromList, selectedId]" in effect_block


# ---------------------------------------------------------------------------
# Executed proof #1: the remount itself (nv-overlays.jsx, real Babel
# transpile + a minimal React.createElement stub - no jsdom needed since
# this only inspects STRUCTURE, never renders to a DOM).
# ---------------------------------------------------------------------------


def _make_stub_context():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("""
        var React = {
          createElement: function(type, props) {
            var children = Array.prototype.slice.call(arguments, 2);
            return { __el: true, type: type, props: props || {}, children: children };
          },
          useEffect: function() {},
          useState: function(iv) { return [iv, function() {}]; },
        };
        var window = this;
        window.React = React;
        function NV_useConsole() {
          return {
            wid: "w1", role: "admin", registry: {},
            openOverlay: function() {}, closeOverlay: function() {},
            setDoc: function() {}, promoteDoc: null,
          };
        }
        window.CollectionsPage = function CollectionsPageMarker() {};
        window.primerApi = {
          toastPush: function() {},
          apiFetch: function() {},
          useResource: function() { return {}; },
        };
    """)
    return ctx


def _transpiled_overlays_src() -> str:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    b = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    return b._transform(OVERLAYS_SRC.read_text(encoding="utf-8"), "components/console/nv-overlays.jsx")


def test_bypasschrome_transition_forces_a_react_remount() -> None:
    """Pins the mechanism itself: NV_LegacyOverlay's two branches return
    DIFFERENT root element types for the SAME overlay name depending on
    whether overlay.id is set. This is what forces React to unmount and
    remount CollectionsPage - not an inference, executed via a real Babel
    transpile of nv-overlays.jsx plus a minimal React.createElement stub
    that records plain-object element trees (no jsdom needed; nothing
    here is rendered to a DOM, only inspected structurally).

    If this test ever starts failing because the two branches return the
    SAME root type, that's good news for the architecture (the remount
    this fix works around would be gone) - but KN_justCreatedCache's
    module-scope fix stays correct either way, so nothing else needs to
    change; this test would just need updating to match.
    """
    code = _transpiled_overlays_src()
    assert code and "NV_LegacyOverlay" in code

    ctx = _make_stub_context()
    ctx.eval(code)
    ctx.eval("""
        var before = NV_LegacyOverlay({ overlay: { name: "collections", id: null, section: null } });
        var after = NV_LegacyOverlay({ overlay: { name: "collections", id: "col-x", section: null } });
        function typeName(t) { return (typeof t === "function") ? (t.name || "(anon fn)") : t; }
        window.__before_type = typeName(before.type);
        window.__after_type = typeName(after.type);
    """)
    before_type = ctx.eval("window.__before_type")
    after_type = ctx.eval("window.__after_type")
    assert before_type == "NV_OverlayPanel", before_type
    assert after_type == "div", after_type
    assert before_type != after_type, (
        "if these ever match, CollectionsPage would no longer remount across "
        "the list<->detail transition - see the docstring above"
    )


def test_collections_page_sits_at_a_different_ancestor_chain_in_each_branch() -> None:
    """Stronger than the root-type check above: walks both element trees
    and confirms CollectionsPage's OWN element (found by identity, not
    just its presence) has a genuinely different ancestor-type chain in
    each branch - not just a different root somewhere far above it."""
    code = _transpiled_overlays_src()
    ctx = _make_stub_context()
    ctx.eval(code)
    ctx.eval("""
        var before = NV_LegacyOverlay({ overlay: { name: "collections", id: null, section: null } });
        var after = NV_LegacyOverlay({ overlay: { name: "collections", id: "col-x", section: null } });
        function typeName(t) { return (typeof t === "function") ? (t.name || "(anon fn)") : t; }
        function findPath(el, path) {
          if (!el || !el.__el) return null;
          var here = path.concat([typeName(el.type)]);
          if (el.type === window.CollectionsPage) return here;
          var kids = el.children || [];
          for (var i = 0; i < kids.length; i++) {
            var k = kids[i];
            if (Array.isArray(k)) {
              for (var j = 0; j < k.length; j++) {
                var r = findPath(k[j], here);
                if (r) return r;
              }
            } else {
              var r = findPath(k, here);
              if (r) return r;
            }
          }
          return null;
        }
        window.__before_path = JSON.stringify(findPath(before, []));
        window.__after_path = JSON.stringify(findPath(after, []));
    """)
    before_path = ctx.eval("window.__before_path")
    after_path = ctx.eval("window.__after_path")
    assert before_path is not None, "CollectionsPage not found in the list-view tree"
    assert after_path is not None, "CollectionsPage not found in the detail-view tree"
    assert before_path != after_path, (
        f"CollectionsPage sits at the same ancestor chain in both branches "
        f"({before_path}) - the remount this fix depends on would not happen"
    )


# ---------------------------------------------------------------------------
# Executed proof #2: the fix itself survives a simulated remount. Extracts
# the addressed-lookup logic (plain JS, no JSX) and drives it in one
# MiniRacer context across two independent "renders" sharing the same
# KN_justCreatedCache object - exactly how two different CollectionsPage
# component instances (before/after the remount) would each see the SAME
# module-scope object.
# ---------------------------------------------------------------------------


def test_addressed_lookup_survives_a_simulated_remount() -> None:
    """Executes the REAL lines from knowledge.jsx (extracted, not
    hand-reimplemented) so a future edit to the actual formula is what
    this test catches, not a stand-in copy of it."""
    src = _src()
    fromlist_line = src[
        src.index("const fromList ="):src.index("\n", src.index("const fromList ="))
    ]
    addressed_line = src[
        src.index("const addressed ="):src.index("\n", src.index("const addressed ="))
    ]
    assert fromlist_line.startswith("const fromList =")
    assert addressed_line.startswith("const addressed =")

    ctx = __import__("py_mini_racer").MiniRacer()
    ctx.eval("var window = this; var KN_justCreatedCache = {};")

    def addressed_for(selected_id: str, rows_json: str) -> None:
        ctx.eval(f"""
            (function() {{
                var selectedId = {selected_id!r};
                var rows = {rows_json};
                {fromlist_line}
                {addressed_line}
                window.__addressed = addressed;
                window.__fromList = fromList || null;
            }})();
        """)

    # "Component instance #1" (before the remount): onCreate's own two
    # writes - stamp the cache, then address into the row - exactly as
    # CollectionsPage's onCreate does in the same synchronous handler.
    ctx.eval("""
        var row = { id: "col-u0025-abc", description: "test" };
        KN_justCreatedCache[row.id] = row;
    """)
    # The list hasn't refetched yet: empty rows, matching the reloadKey-
    # bump race round 1 diagnosed correctly.
    addressed_for("col-u0025-abc", "[]")
    assert ctx.eval("window.__addressed") == {"id": "col-u0025-abc", "description": "test"}
    assert ctx.eval("window.__fromList") is None

    # "Component instance #2" (after the remount): a FRESH read against
    # the SAME KN_justCreatedCache (module scope, never reset) - this is
    # the exact case round 1's React-state fallback could not survive.
    # Still no list refetch yet.
    addressed_for("col-u0025-abc", "[]")
    assert ctx.eval("window.__addressed") == {"id": "col-u0025-abc", "description": "test"}, (
        "the cache must resolve the row on a fresh read after a simulated "
        "remount - this is the exact case the round-1 fix (React state) "
        "could not survive"
    )

    # The list catches up: fromList now wins (list-derived lookup stays
    # authoritative once available), and the effect (tested separately
    # via static source above) would delete the cache entry.
    addressed_for("col-u0025-abc", '[{"id": "col-u0025-abc", "description": "test"}]')
    assert ctx.eval("window.__fromList") == {"id": "col-u0025-abc", "description": "test"}
    assert ctx.eval("window.__addressed") == {"id": "col-u0025-abc", "description": "test"}
