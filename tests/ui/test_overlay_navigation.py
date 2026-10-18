"""Overlay navigation: Delete and Back close the overlay and land on the Platform page, never on a second list (revamp design 10, D1).

A detail page's Delete and Back called ``navigate("/<plural>")``. The router shim turned that into ``openOverlay("<plural>")`` with no record, so the
overlay rendered the old LIST page: a second copy of the Platform page, with its own "Create ..." button, on top of the first (the user saw it for graphs,
and for triggers). The shim now reads a bare list path as "close the overlay and select that entity's Platform page" (``SH_SHIM_LIST_TARGETS``), and a
session path as "open that session in the studio" (it used to reopen the current overlay with the session id in a slot nothing reads).

The shim and the shell's two navigations (``closeOverlayTo``, ``openSessionInStudio``) run as the real source in V8. The overlay host, the crumb and the
approvals editor mount the real ``nv-overlays.jsx`` in the small React stand-in (``tests/ui/_mini_react.py``), so a handler that calls the wrong method
fails here and not only in the browser. The browser journeys are ``tests/ui_e2e/test_overlay_navigation_journey.py`` (lead-scheduled).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
URL_JS = (UI / "foundation" / "shell-url.js").read_text(encoding="utf-8")
SHIM_JS = (UI / "foundation" / "shell-router-shim.js").read_text(encoding="utf-8")
SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
OVERLAYS_PATH = UI / "components" / "console" / "nv-overlays.jsx"
CATALOG = (UI / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")
CSS = (UI / "styles.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The shim, as the real source
# ---------------------------------------------------------------------------


def _shim_ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(URL_JS)  # SH_OVERLAYS: the shim reads the overlay names it hosts
    ctx.eval(SHIM_JS)
    return ctx


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def navigate(overlay, path: str, opts=None, *, close_to: bool = True, open_session: bool = True) -> list:
    """``SH_shimNavigate`` with recording callbacks; the calls it made, in order."""
    ctx = _shim_ctx()
    try:
        return js(
            ctx,
            "(function () { var calls = [];"
            " var open = function (n, s, i) { calls.push(['open', n, s, i]); };"
            " var closeTo = function (t) { calls.push(['closeTo', t]); };"
            " var openSession = function (sid) { calls.push(['openSession', sid]); };"
            f" SH_shimNavigate({json.dumps(overlay)}, open, {json.dumps(path)}, {json.dumps(opts)},"
            f" {'closeTo' if close_to else 'undefined'}, {'openSession' if open_session else 'undefined'});"
            " return calls; })()",
        )
    finally:
        ctx.close()


LIST_TARGETS = [
    ("graphs", {"name": "platform", "nav": "graphs", "sub": None}),
    ("agents", {"name": "platform", "nav": "agents", "sub": None}),
    ("toolsets", {"name": "platform", "nav": "toolsets", "sub": None}),
    ("triggers", {"name": "platform", "nav": "triggers", "sub": None}),
    ("harnesses", {"name": "platform", "nav": "harnesses", "sub": None}),
    ("services", {"name": "platform", "nav": "services", "sub": None}),
    ("approvals", {"name": "platform", "nav": "approvals", "sub": None}),
    ("workspaces", {"name": "studio", "nav": None, "sub": None}),
]


@pytest.mark.parametrize("plural,target", LIST_TARGETS)
def test_a_bare_list_path_closes_the_overlay_to_its_page(plural, target):
    calls = navigate({"name": plural, "section": None, "id": "x1"}, "/" + plural)
    assert calls == [["closeTo", target]], "no list page may render inside an overlay (design 10)"


@pytest.mark.parametrize("plural,target", LIST_TARGETS)
def test_the_target_is_the_entitys_page_whatever_overlay_is_open_and_a_trailing_slash_is_ignored(plural, target):
    """Review Focus 2: an overlay opened by a pasted link or the palette sits over the STUDIO, not over its own Platform page. The shim knows nothing of what is under the overlay; the
    target is a function of the path alone (here an unrelated agent overlay is the open one, and the path has a trailing slash)."""
    assert navigate({"name": "agents", "section": None, "id": "x1"}, "/" + plural + "/") == [["closeTo", target]]


def test_record_and_tab_paths_still_open_the_overlay():
    assert navigate({"name": "graphs", "id": "g1"}, "/graphs/g2") == [["open", "graphs", None, "g2"]]
    assert navigate({"name": "toolsets", "id": "t1"}, "/toolsets/t1", {"tab": "tools"}) == [["open", "toolsets", "tools", "t1"]]
    assert navigate({"name": "agents", "id": "a1"}, "/agents?tab=tools") == [["open", "agents", "tools", "a1"]]
    assert navigate({"name": "services", "id": "s1"}, "/services/s1?tab=versions") == [["open", "services", "versions", "s1"]]


def test_a_tab_on_a_list_path_is_not_a_list_close():
    """The tab is what the page is asking for; it stays an overlay open."""
    assert navigate({"name": "agents", "id": "a1"}, "/agents", {"tab": "tools"}) == [["open", "agents", "tools", "a1"]]


def test_a_path_outside_the_table_keeps_todays_behaviour():
    assert navigate({"name": "collections", "id": "c1"}, "/collections") == [["open", "collections", None, None]]
    assert navigate({"name": "providers", "section": "llm", "id": "p1"}, "/providers") == [["open", "providers", None, None]]


def test_without_close_to_the_shim_falls_back_to_opening_the_list():
    assert navigate({"name": "graphs", "id": "g1"}, "/graphs", close_to=False) == [["open", "graphs", None, None]]


@pytest.mark.parametrize("path", ["/constructor", "/__proto__", "/toString", "/hasOwnProperty", "/workspaces/constructor"])
def test_an_object_prototype_key_is_not_a_list_target(path):
    calls = navigate({"name": "agents", "id": "a1"}, path)
    assert all(call[0] != "closeTo" for call in calls), calls


def test_the_list_targets_are_on_window_and_name_pages_the_grammar_accepts():
    """A row whose nav the URL parser would drop lands on the default view with a canonical URL that names nothing."""
    ctx = _shim_ctx()
    try:
        table = js(ctx, "window.SH_SHIM_LIST_TARGETS")
        views = js(ctx, "SH_VIEWS")
        overlays = js(ctx, "SH_OVERLAYS")
    finally:
        ctx.close()
    provider_types = set(re.findall(r'\{ key: "(\w+)", label:', CATALOG))
    assert {"workspace", "channel", "llm"} <= provider_types, "the pattern must still read the provider catalogue's registry"
    for path, target in table.items():
        assert path.split("/")[0] in overlays, f"{path}: the shim only reaches a path whose first segment is an overlay it hosts"
        assert target["name"] in views, path
        if target["name"] == "platform":
            assert target["nav"] in views["platform"], path
        else:
            assert target["nav"] is None, path
        if target.get("sub"):
            assert target["nav"] == "providers" and target["sub"] in provider_types, path
    for plural, expected in LIST_TARGETS:
        assert table[plural]["name"] == expected["name"] and table[plural]["nav"] == expected["nav"], plural


def test_list_target_is_a_copy_so_a_caller_cannot_edit_the_table():
    ctx = _shim_ctx()
    try:
        out = js(
            ctx,
            "(function () { var t = SH_shimListTarget('/agents'); t.nav = 'tampered';"
            " return [SH_shimListTarget('/agents').nav, window.SH_SHIM_LIST_TARGETS.agents.nav]; })()",
        )
    finally:
        ctx.close()
    assert out == ["agents", "agents"]


def test_install_passes_close_to_into_navigate():
    ctx = _shim_ctx()
    try:
        calls = js(
            ctx,
            "(function () { var calls = []; var overlay = { name: 'triggers', section: null, id: 't1' };"
            " SH_installRouterShim(function () { return overlay; },"
            "   function (n, s, i) { calls.push(['open', n, s, i]); },"
            "   function (t) { calls.push(['closeTo', t]); });"
            " window.primerApi.useRouter().navigate('/triggers');"
            " return calls; })()",
        )
    finally:
        ctx.close()
    assert calls == [["closeTo", {"name": "platform", "nav": "triggers", "sub": None}]]


def test_install_without_close_to_keeps_todays_behaviour():
    ctx = _shim_ctx()
    try:
        calls = js(
            ctx,
            "(function () { var calls = []; var overlay = { name: 'triggers', section: null, id: 't1' };"
            " SH_installRouterShim(function () { return overlay; }, function (n, s, i) { calls.push(['open', n, s, i]); });"
            " window.primerApi.useRouter().navigate('/triggers');"
            " return calls; })()",
        )
    finally:
        ctx.close()
    assert calls == [["open", "triggers", None, None]]


# ---------------------------------------------------------------------------
# Workspace templates and workspace providers (their lists are two segments: "workspaces/templates", "workspaces/providers")
# ---------------------------------------------------------------------------

TEMPLATES_PAGE = {"name": "platform", "nav": "templates", "sub": None}
WORKSPACE_PROVIDERS_PAGE = {"name": "platform", "nav": "providers", "sub": "workspace"}


@pytest.mark.parametrize(
    "overlay",
    [
        {"name": "workspaces", "section": "templates", "id": "t1"},
        {"name": "providers", "section": "workspace", "id": "p1"},
        {"name": "workspaces", "section": "providers", "id": "p1"},
        None,
    ],
    ids=["template-overlay", "catalog-overlay", "provider-overlay", "no-overlay"],
)
def test_deleting_a_workspace_template_lands_on_the_templates_page(overlay):
    """It used to read the single trailing segment as a workspace id and open "workspace not found" (``/workspaces/templates`` -> the record "templates")."""
    assert navigate(overlay, "/workspaces/templates") == [["closeTo", TEMPLATES_PAGE]]


@pytest.mark.parametrize(
    "overlay",
    [
        {"name": "workspaces", "section": "providers", "id": "p1"},
        {"name": "providers", "section": "workspace", "id": "p1"},
        {"name": "workspaces", "section": "templates", "id": "t1"},
        None,
    ],
    ids=["provider-overlay", "catalog-overlay", "template-overlay", "no-overlay"],
)
def test_deleting_a_workspace_provider_lands_on_the_workspace_providers_page(overlay):
    assert navigate(overlay, "/workspaces/providers") == [["closeTo", WORKSPACE_PROVIDERS_PAGE]]


def test_the_records_of_templates_and_providers_and_workspaces_still_open_their_overlays():
    assert navigate({"name": "workspaces", "id": "t1"}, "/workspaces/templates/t1") == [["open", "workspaces", "templates", "t1"]]
    assert navigate({"name": "workspaces", "id": "t1"}, "/workspaces/providers/p1") == [["open", "workspaces", "providers", "p1"]]
    assert navigate({"name": "workspaces", "id": "t1"}, "/workspaces/ws-9") == [["open", "workspaces", None, "ws-9"]]


# ---------------------------------------------------------------------------
# A session is a studio document, not an overlay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overlay",
    [
        {"name": "agents", "section": None, "id": "a1"},
        {"name": "toolsets", "section": "sessions", "id": "t1"},
        {"name": "workspaces", "section": None, "id": "w1"},
        None,
    ],
    ids=["agent", "toolset", "workspace", "no-overlay"],
)
def test_a_session_path_opens_the_session_in_the_studio(overlay):
    """Chat on an agent, a session row on a toolset or a workspace: ``/sessions/<id>`` used to reopen the CURRENT overlay with the session id in its tab slot."""
    assert navigate(overlay, "/sessions/ses-1") == [["openSession", "ses-1"]]


def test_a_session_path_without_the_opener_keeps_todays_behaviour():
    assert navigate({"name": "agents", "id": "a1"}, "/sessions/ses-1", open_session=False) == [["open", "agents", "sessions", "ses-1"]]


@pytest.mark.parametrize("path", ["/sessions", "/sessions/", "/sessions/ses-1/extra"])
def test_only_a_session_and_its_id_is_a_session_open(path):
    calls = navigate({"name": "agents", "id": "a1"}, path)
    assert all(call[0] != "openSession" for call in calls), calls


def test_install_passes_open_session_into_navigate():
    ctx = _shim_ctx()
    try:
        calls = js(
            ctx,
            "(function () { var calls = []; var overlay = { name: 'agents', section: null, id: 'a1' };"
            " SH_installRouterShim(function () { return overlay; }, function (n, s, i) { calls.push(['open', n, s, i]); },"
            "   function (t) { calls.push(['closeTo', t]); }, function (sid) { calls.push(['openSession', sid]); });"
            " window.primerApi.useRouter().navigate('/sessions/ses-7');"
            " return calls; })()",
        )
    finally:
        ctx.close()
    assert calls == [["openSession", "ses-7"]]


# ---------------------------------------------------------------------------
# The shell's two navigations, as the real source
# ---------------------------------------------------------------------------


def _slice_callback(src: str, start: str) -> str:
    """A ``useCallback`` definition: from its opening line through the dependency array that closes it."""
    i = src.index(start)
    deps = src.index("\n  }, [", i)
    return src[i : src.index("]);", deps) + len("]);")]


def _run_shell_source(source: str, tail: str, meta: dict | None = None) -> list:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        out = ctx.eval(
            "(function () { var calls = []; var window = globalThis;"
            " var META = " + json.dumps(meta or {}) + ";"
            " var markPush = function () { calls.push(['markPush']); };"
            " var setOverlay = function (v) { calls.push(['setOverlay', v]); };"
            " var setView = function (v) { calls.push(['setView', v]); };"
            " var setWid = function (v) { calls.push(['setWid', v]); };"
            " var setTgModel = function (fn) { calls.push(['setTgModel', fn('MODEL')]); };"
            " window.TG_openTab = function (m, d, o) { return ['openTab', m, d, o]; };"
            " var resolveSessionMeta = function (sid) { return META[sid]; };"
            " var React = { useCallback: function (fn) { return fn; } };"
            + source
            + tail
            + " return JSON.stringify(calls); })()"
        )
        return json.loads(out)
    finally:
        ctx.close()


CLOSE_START = "  var closeOverlayTo = React.useCallback(function (target) {"
SESSION_START = "  var openSessionInStudio = React.useCallback(function (sid) {"


def test_close_overlay_to_is_one_history_entry():
    src = _slice_callback(SHELL, CLOSE_START)
    calls = _run_shell_source(src, " closeOverlayTo({ name: 'platform', nav: 'graphs', sub: null });")
    assert calls == [["markPush"], ["setOverlay", None], ["setView", {"name": "platform", "nav": "graphs", "sub": None}]], (
        "one markPush (one history entry), the overlay dropped, the view set: nothing else"
    )


def test_close_overlay_to_passes_the_provider_type_and_defaults_the_rest():
    src = _slice_callback(SHELL, CLOSE_START)
    typed = _run_shell_source(src, " closeOverlayTo({ name: 'platform', nav: 'providers', sub: 'workspace' });")
    assert typed[-1] == ["setView", {"name": "platform", "nav": "providers", "sub": "workspace"}]
    bare = _run_shell_source(src, " closeOverlayTo({ name: 'studio' });")
    assert bare[-1] == ["setView", {"name": "studio", "nav": None, "sub": None}]


def test_open_session_in_studio_is_one_history_entry_that_lands_on_a_promoted_session_tab():
    src = _slice_callback(SHELL, SESSION_START)
    calls = _run_shell_source(src, " openSessionInStudio('ses-1');")
    assert calls == [
        ["markPush"],
        ["setOverlay", None],
        ["setView", {"name": "studio", "nav": None, "sub": None}],
        ["setTgModel", ["openTab", "MODEL", {"kind": "session", "ref": "ses-1"}, {"promote": True}]],
    ], "one markPush; the overlay dropped; the studio selected; the session opened as a promoted tab (an unknown workspace leaves wid alone)"


def test_open_session_in_studio_follows_the_session_to_its_workspace_when_the_rail_knows_it():
    """The palette and the rail do the same: the files sidebar, the terminal and the rail follow the session's workspace."""
    src = _slice_callback(SHELL, SESSION_START)
    calls = _run_shell_source(src, " openSessionInStudio('ses-1');", meta={"ses-1": {"wid": "w2", "name": "n"}})
    assert calls[0] == ["markPush"] and calls.count(["markPush"]) == 1
    assert ["setWid", "w2"] in calls


def test_open_session_in_studio_without_an_id_does_nothing():
    src = _slice_callback(SHELL, SESSION_START)
    assert _run_shell_source(src, " openSessionInStudio('');") == []


def test_the_context_hands_both_navigations_to_the_surfaces():
    for name in ("closeOverlayTo", "openSessionInStudio"):
        export = f"{name}: {name},"
        assert export in SHELL, f"the context value exports {name}"
        # The memo's dependency list is the array that closes the context value; it must name the callback, or a surface would keep a stale one.
        memo = SHELL[SHELL.index(export) :]
        deps = memo[memo.index("}, [") : memo.index("]);", memo.index("}, ["))]
        assert name in deps, f"and the context memo depends on {name}"


# ---------------------------------------------------------------------------
# The overlay host, the crumb and the approvals editor: the real nv-overlays.jsx
# ---------------------------------------------------------------------------

_PRELUDE = (
    "var window = globalThis;\n"
    + URL_JS
    + "\n"
    + SHIM_JS
    + r"""
var LOG = { closeTo: [], open: [], closed: 0, sessions: [] };
var __con = {
  wid: "w1", role: "admin", overlay: null, registry: { get: function () { return null; } },
  openOverlay: function (n, s, i) { LOG.open.push([n, s || null, i || null]); },
  closeOverlay: function () { LOG.closed += 1; },
  closeOverlayTo: function (v) { LOG.closeTo.push(v); },
  openSessionInStudio: function (sid) { LOG.sessions.push(sid); },
  setDoc: function () {}, promoteDoc: function () {},
};
function NV_useConsole() { return __con; }
window.primerApi = { useFocusTrap: function () {}, focusablesOf: function () { return []; }, useEscape: function () {} };
function __stub(name) { return function () { return React.createElement("div", { "data-testid": "stub:" + name }); }; }
["AgentDetail", "GraphDetail", "ToolsetDetail", "TR_TriggersPage", "HarnessesPage", "SV_ServicesPage", "ProviderCatalog", "WorkspaceTemplateDetail", "WorkspaceProviderDetail", "WorkspaceDetail"].forEach(function (n) {
  window[n] = __stub(n);
});
window.AP_PolicyDetail = function (props) { window.__apProps = props; return React.createElement("div", { "data-testid": "stub:policy" }); };
"""
)


@pytest.fixture(scope="module")
def overlays_code() -> str:
    return transpile(OVERLAYS_PATH)


@pytest.fixture
def host(overlays_code):
    ctx = mini_react_context(overlays_code, _PRELUDE)

    class Host:
        def mount(self, overlay: dict, component: str = "NV_LegacyOverlay") -> None:
            ctx.eval("__con.overlay = " + json.dumps(overlay) + ";")
            props = "{ overlay: __con.overlay }" if component == "NV_LegacyOverlay" else "{}"
            ctx.eval(f"MR.mount({component}, {props})")

        def click(self, testid: str) -> None:
            ctx.eval(f"MR.click({json.dumps(testid)})")

        def log(self) -> dict:
            return js(ctx, "LOG")

        def eval(self, expr: str):
            return js(ctx, expr)

    yield Host()
    ctx.close()


def test_the_overlay_host_installs_the_shim_over_the_consoles_two_navigations(host):
    host.mount({"name": "agents", "section": None, "id": "a1"}, "NV_OverlayHost")
    host.eval("(function () { var r = window.primerApi.useRouter(); r.navigate('/agents'); r.navigate('/sessions/ses-3'); r.navigate('/agents/a2'); return 1; })()")
    log = host.log()
    assert log["closeTo"] == [{"name": "platform", "nav": "agents", "sub": None}], "a bare list path closes the overlay to its Platform page"
    assert log["sessions"] == ["ses-3"], "a session path opens the session in the studio"
    assert log["open"] == [["agents", None, "a2"]], "a record path still opens the record"


def test_the_host_reaches_the_current_console_not_the_one_it_was_installed_with(host):
    """The shim is installed once, on the first render; a navigation after a re-render must reach that render's console."""
    host.mount({"name": "agents", "section": None, "id": "a1"}, "NV_OverlayHost")
    host.eval(
        "(function () { __con = Object.assign({}, __con, { closeOverlayTo: function (v) { LOG.closeTo.push(['second', v.nav]); } });"
        " MR.rerender(); window.primerApi.useRouter().navigate('/triggers'); return 1; })()"
    )
    assert host.log()["closeTo"] == [["second", "triggers"]]


@pytest.mark.parametrize("plural,target", [(p, t) for p, t in LIST_TARGETS if p != "agents"])
def test_the_crumb_closes_to_the_platform_page_instead_of_opening_the_list(host, plural, target):
    """The agent editor draws its own Modal chrome (no crumb); every other detail overlay has one."""
    host.mount({"name": plural, "section": None, "id": "x1"})
    host.click("nv-overlay-crumb")
    log = host.log()
    assert log["closeTo"] == [target], "the crumb of a detail overlay leaves the overlay (design 10)"
    assert log["open"] == [], "and does not open the list as an overlay"


@pytest.mark.parametrize(
    "overlay,target",
    [
        ({"name": "workspaces", "section": "templates", "id": "t1"}, TEMPLATES_PAGE),
        ({"name": "workspaces", "section": "providers", "id": "p1"}, WORKSPACE_PROVIDERS_PAGE),
        ({"name": "toolsets", "section": "tools", "id": "t1"}, {"name": "platform", "nav": "toolsets", "sub": None}),
    ],
    ids=["template", "workspace-provider", "toolset-tab"],
)
def test_the_crumb_of_a_section_overlay_goes_to_that_sections_page(host, overlay, target):
    """``workspaces:templates:<id>`` is a template, not a workspace: its crumb must not send the user to the studio; a tab section is not a page."""
    host.mount(overlay)
    host.click("nv-overlay-crumb")
    assert host.log()["closeTo"] == [target]


def test_a_crumb_with_no_platform_page_keeps_opening_the_overlay_root(host):
    """Providers is Plan 02's surface: no row in the table yet, so today's behaviour stands (the table's comment says so)."""
    host.mount({"name": "providers", "section": "llm", "id": "pv-1"})
    host.click("nv-overlay-crumb")
    log = host.log()
    assert log["open"] == [["providers", None, None]] and log["closeTo"] == []


def test_the_approvals_editor_closes_to_the_approvals_page_not_to_a_list_overlay(host):
    host.mount({"name": "approvals", "section": None, "id": "p1"})
    host.eval("(function () { window.__apProps.onClose(); return 1; })()")
    log = host.log()
    assert log["closeTo"] == [{"name": "platform", "nav": "approvals"}]
    assert log["open"] == [], "the editor's close used to open the approvals LIST as an overlay"
    assert log["closed"] == 0, "closeOverlayTo does the closing itself; the adapter forwards to it and adds no second step"


def _css_z_indexes(selector: str) -> set[int]:
    """Every z-index declared by a top-level rule whose selector is exactly ``selector``."""
    found: set[int] = set()
    for rule in re.finditer(r"^" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS, re.M):
        found.update(int(z) for z in re.findall(r"z-index:\s*(\d+)", rule.group(1)))
    return found


@pytest.mark.parametrize(
    "overlay",
    [{"name": "agents", "section": None, "id": "a1"}, {"name": "collections", "section": None, "id": "c1"}],
    ids=["agent", "collection"],
)
def test_the_bypass_wrappers_are_on_the_overlay_layer_above_the_topbar(host, overlay):
    """The agent editor and the collection browser draw their own Modal, inside two bare ``position: fixed`` wrappers. A fixed element is its own stacking context, so with
    no z-index the wrappers painted at level 0, under the topbar (60): at 1366x768 the editor's header Close sat beneath it and could not be clicked (CI, and a real user)."""
    host.mount(overlay)
    layer = host.eval("MR.find('nv-overlay-body').props.style.zIndex")
    assert isinstance(layer, int), "the outer wrapper must carry a z-index"
    for testid in ("nv-overlay-body", "nv-overlay:" + overlay["name"]):
        style = host.eval("MR.find(" + json.dumps(testid) + ").props.style")
        assert style == {"position": "fixed", "inset": 0, "zIndex": layer}, testid

    topbar, scrim = _css_z_indexes(".nv-topbar"), _css_z_indexes(".nv-scrim")
    shared_dialogs, toasts = _css_z_indexes(".modal-overlay"), _css_z_indexes(".toast-stack")
    assert topbar and scrim and shared_dialogs and toasts, "the pattern must still read styles.css"
    assert layer > max(topbar), "above the topbar, or its header covers the editor's"
    assert {layer} == scrim, "the layer .nv-scrim uses: every other overlay in this file sits on it"
    assert layer < min(shared_dialogs) and layer < min(toasts), "below the shared dialogs a page opens from inside (the delete confirm) and the toasts"


# ---------------------------------------------------------------------------
# The three details that had no toast: Delete says what happened before the page leaves
# ---------------------------------------------------------------------------

TRIGGERS_SRC = (UI / "components" / "triggers.jsx").read_text(encoding="utf-8")
HARNESSES_SRC = (UI / "components" / "harnesses.jsx").read_text(encoding="utf-8")
SERVICES_SRC = (UI / "components" / "services.jsx").read_text(encoding="utf-8")


def _slice_statement(src: str, start: str, end: str, after: str | None = None) -> str:
    base = src.index(after) if after else 0
    i = src.index(start, base)
    return src[i : src.index(end, i) + len(end)]


def _run_delete(source: str, tail: str, setup: str = "") -> list:
    """Run a page's real delete code with recording stand-ins; the calls it made, in order."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        ctx.eval(
            "var calls = []; var window = globalThis;"
            " window.primerApi = { toastPush: function (t) { calls.push(['toastPush', t]); } };"
            " var navigate = function (p) { calls.push(['navigate', p]); };"
            " var apiFetch = function (m, p) { calls.push(['apiFetch', m, p]); return Promise.resolve({}); };"
            " var noop = function () {};"
            + setup
            + source
            + tail
        )
        # The handlers are async: V8 runs the microtasks between evals, so the second read sees the finished sequence.
        return json.loads(ctx.eval("JSON.stringify(calls)"))
    finally:
        ctx.close()


def test_a_deleted_trigger_toasts_before_the_page_leaves():
    src = _slice_statement(TRIGGERS_SRC, "  const doDelete = async () => {", "\n  };", after="function TR_TriggerDetail(")
    calls = _run_delete(
        src,
        " doDelete();",
        setup=(
            " var id = 't1'; var setDeleteBusy = noop; var setDeleteError = noop;"
            " var detail = { data: { name: 'Nightly', slug: 'nightly' } };"
        ),
    )
    assert calls == [
        ["apiFetch", "DELETE", "/triggers/t1"],
        ["toastPush", {"kind": "warning", "title": "Trigger deleted", "detail": "Nightly"}],
        ["navigate", "/triggers"],
    ]


def test_a_deleted_trigger_with_no_name_is_named_by_its_slug_then_its_id():
    src = _slice_statement(TRIGGERS_SRC, "  const doDelete = async () => {", "\n  };", after="function TR_TriggerDetail(")
    setup = " var id = 't1'; var setDeleteBusy = noop; var setDeleteError = noop;"
    slug = _run_delete(src, " doDelete();", setup + " var detail = { data: { slug: 'nightly' } };")
    assert slug[1][1]["detail"] == "nightly"
    bare = _run_delete(src, " doDelete();", setup + " var detail = { data: null };")
    assert bare[1][1]["detail"] == "t1"


def test_a_refused_trigger_delete_neither_toasts_nor_leaves():
    src = _slice_statement(TRIGGERS_SRC, "  const doDelete = async () => {", "\n  };", after="function TR_TriggerDetail(")
    calls = _run_delete(
        src,
        " doDelete();",
        setup=(
            " var id = 't1'; var setDeleteBusy = noop; var setDeleteError = function (e) { calls.push(['error', !!e]); };"
            " var detail = { data: null };"
            " apiFetch = function () { return Promise.reject(new Error('409')); };"
        ),
    )
    assert calls == [["error", False], ["error", True]], "the refusal is shown in the dialog, and the detail stays"


UNINSTALL_START = "  const uninstallMut = useMutation("


def _uninstall(direction: str) -> list:
    src = _slice_statement(HARNESSES_SRC, UNINSTALL_START, "\n  );", after="function HarnessDetail(")
    return _run_delete(
        src,
        " uninstallMut.onSuccess();",
        setup=(
            " var id = 'h1'; var cascadeDelete = false;"
            " var useMutation = function (fn, opts) { return opts; };"
            " var detail = { data: { direction: '" + direction + "', name: 'Ops pack', slug: 'ops' }, refetch: noop };"
        ),
    )


def test_an_uninstalled_harness_toasts_before_the_page_leaves():
    assert _uninstall("inbound") == [
        ["toastPush", {"kind": "warning", "title": "Harness uninstalled", "detail": "Ops pack"}],
        ["navigate", "/harnesses"],
    ]


def test_a_deleted_outbound_harness_says_deleted():
    """The button says Delete for an outbound harness and Uninstall for an inbound one; the toast follows it."""
    assert _uninstall("outbound")[0][1]["title"] == "Harness deleted"


def test_a_deleted_service_toasts_before_the_page_leaves():
    src = _slice_statement(SERVICES_SRC, "  const doDelete = async () => {", "\n  };", after="function SV_ServiceDetail(")
    calls = _run_delete(
        src,
        " doDelete();",
        setup=(
            " var serviceId = 'sv-1'; var svc = { name: 'report' }; var setError = noop; var setConfirmDelete = noop;"
            " var SV_extractError = function (e) { return e; };"
        ),
    )
    assert calls == [
        ["apiFetch", "DELETE", "/services/sv-1"],
        ["toastPush", {"kind": "warning", "title": "Service deleted", "detail": "report"}],
        ["navigate", "/services"],
    ]


# ---------------------------------------------------------------------------
# No bare list path is left that opens a list overlay without being named
# ---------------------------------------------------------------------------

# Paths a page navigates to that would still open a LIST overlay, with the owner and reason. A new bare list navigation must either get a row in
# SH_SHIM_LIST_TARGETS (so it closes to the Platform page) or be added here with the reason it is exempt.
NOT_YET_REDIRECTED = {
    "providers": "the Providers catalogue is Plan 02's surface (the Register overlay); its row is added there",
    "channels/providers": "a channel provider's detail inside the Providers catalogue: Plan 02",
}

_NAVIGATE_LITERAL = re.compile(r"""navigate\(\s*["'`]/([a-z][a-z-]*(?:/[a-z][a-z-]*)?)["'`]\s*[,)]""")


def test_every_literal_list_navigation_in_the_pages_is_redirected_or_listed():
    ctx = _shim_ctx()
    try:
        table = set(js(ctx, "Object.keys(window.SH_SHIM_LIST_TARGETS)"))
        overlays = set(js(ctx, "SH_OVERLAYS"))
    finally:
        ctx.close()
    found: dict[str, set[str]] = {}
    for path in sorted((UI / "components").rglob("*.jsx")):
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
        for match in _NAVIGATE_LITERAL.finditer(text):
            target = match.group(1)
            first = target.split("/")[0]
            if first in overlays:
                found.setdefault(target, set()).add(path.name)
    known = {"agents", "graphs", "toolsets", "triggers", "harnesses", "services", "workspaces", "workspaces/templates", "workspaces/providers", "providers"}
    assert known <= set(found), f"the scan no longer finds the pages' navigations (missing {sorted(known - set(found))}); fix the pattern, not the table"
    unnamed = {t: sorted(files) for t, files in found.items() if t not in table and t not in NOT_YET_REDIRECTED}
    assert not unnamed, f"these list paths would open a list inside an overlay; add them to SH_SHIM_LIST_TARGETS: {unnamed}"
    for target in NOT_YET_REDIRECTED:
        assert target not in table, f"{target} is redirected now: take it out of the exemption list"
