"""The mobile More tab reaches what the desktop reaches (lead sweep M1, review ADM-29 and ADM-30).

Before: a deep-linked platform surface (``?overlay=agents``) opened on the More tab BELOW the profile card, the theme toggle, four
health cards and a PLATFORM header (the list started about 1300px down), a phone could not sign out, open System settings or open
Providers, and ``?view=platform:*`` / ``?view=system:*`` links left the shell on Inbox.

These run the real ``nv-mobile-shell.jsx`` (transpiled the way the server bundles it) in V8 on the small hook runtime in
``tests/ui/_mini_react.py``: components are mounted, buttons are clicked, and the assertions read what was rendered and what was
called. A source grep cannot tell that a handler uses the wrong method or that an effect no longer fires, which is what the first
version of this file let through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx"
# The theme helpers the phone shares with the desktop menu (NV_themeChoice, NV_applyThemeChoice, NV_useThemeRestore) live in the chrome.
CHROME = ROOT / "ui" / "components" / "console" / "nv-chrome.jsx"
PLATFORM = ROOT / "ui" / "components" / "console" / "nv-platform.jsx"
# SH_pendingId, which keys the Inbox cards by their gate (C-033): a global of the shell bundle the real component reads.
ATTENTION = (ROOT / "ui" / "foundation" / "shell-attention.js").read_text(encoding="utf-8")
# The real grammar: the phone's list of provider types is the sidebar's (window.SH_VIEW_SUBS), so a stand-in list would test the stand-in.
SHELL_URL = (ROOT / "ui" / "foundation" / "shell-url.js").read_text(encoding="utf-8")

# What the file's globals need to exist; each is the smallest stand-in that lets the real component render.
_PRELUDE = r"""
var LOG = { fetches: [], reloads: 0, overlays: [], views: [], closedOverlays: 0, navReports: [], consumed: 0, tabs: [], cleared: 0, tweaks: [] };
var ITEMS = [];
var LOADING = false;
var __con = {
  username: "ana", role: "admin", wid: "w1", doc: null, overlay: null, view: { name: "studio", nav: null, sub: null },
  openOverlay: function (name) { LOG.overlays.push(name); },
  clearView: function () { LOG.cleared += 1; URLVIEW = { name: "studio", nav: null, sub: null }; __con = Object.assign({}, __con, { view: fresh() }); },
  closeOverlay: function () { LOG.closedOverlays += 1; },
  goView: function (name, nav) { LOG.views.push([name, nav || null]); },
  toast: function () {}, bump: function () {},
};
function NV_useConsole() { return __con; }
// The Inbox card draws the agent glyph (nv-studio.jsx); a stand-in with the two fields the card reads.
function NV_identity() { return { color: "#888", d: "M0 0" }; }
// The view as the URL names it, and what the shell does with it. goView writes the URL and hands over a NEW view object; a hashchange or
// popstate (nv-shell's onNav) re-parses the URL into another FRESH object without any goView; clearView drops the view from state, and
// so from the URL, as a replace (no history entry). The view carries the grammar's third segment, the provider type (seam I-1).
var URLVIEW = { name: "studio", nav: null, sub: null };
function fresh() { return { name: URLVIEW.name, nav: URLVIEW.nav, sub: URLVIEW.sub }; }
function navigate(name, nav, sub) { URLVIEW = { name: name, nav: nav || null, sub: sub || null }; __con = Object.assign({}, __con, { view: fresh() }); }
function reparse() { __con = Object.assign({}, __con, { view: fresh() }); }
// Where window.ProviderCatalog mounts: what it was asked to open is what the phone decided, and mounts and unmounts count how often an instance came and went (the real
// catalogue keeps its class and its open instance in state, which a new instance starts empty).
var CATALOG = { seen: [], props: null, mounts: 0, unmounts: 0 };
window.ProviderCatalog = function (p) {
  CATALOG.props = p;
  React.useEffect(function () { CATALOG.mounts += 1; return function () { CATALOG.unmounts += 1; }; }, []);
  CATALOG.seen.push({ initialClass: p.initialClass, initialInstanceId: p.initialInstanceId === null ? "null" : typeof p.initialInstanceId });
  return React.createElement("div", { "data-testid": "catalog-stand-in", "data-class": p.initialClass });
};
// A custom page's component (what Plans 04 and 08 export on window), and the text of an element tree for a row's label.
var STAND_IN_PROPS = [];
function STAND_IN(id) {
  return function (props) { STAND_IN_PROPS.push(Object.keys(props || {})); return React.createElement("div", { "data-testid": id }); };
}
function TXT(n) {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(TXT).join("");
  return n.__el ? TXT(n.children) : "";
}
var NV_HealthCards = function () { return React.createElement("div", { "data-testid": "health-cards" }); };
var NV_PLAT_GROUPS = [{ label: "Build", ids: ["agents", "toolsets"] }];
window.NV_PLAT_PAGES = {
  agents: { title: "Agents", list: function () {}, card: function (row) { return { id: row.id, name: "agent " + row.id, sub: "", facts: [] }; } },
  toolsets: { title: "Toolsets", list: function () {}, card: function (row) { return { id: row.id, name: "toolset " + row.id, sub: "", facts: [] }; } },
};
window.primerApi = {
  apiFetch: function () {},
  useResource: function () {
    return { data: { items: ITEMS }, loading: LOADING, error: null, degraded: false, refetch: function () {} };
  },
};
// The tweaks store's setter, recorded: the theme helpers persist through it (ui/foundation/tweaks.js has its own tests).
window.primerApi.setTweak = function (k, v) { LOG.tweaks.push([k, v]); };
window.useWorkspaceTapListener = function () {};
var SH_api = { pendingAttention: function () { return Promise.resolve({ items: [] }); } };
window.BottomSheet = function (props) {
  return React.createElement("div", { "data-testid": "sheet", "data-open": String(!!props.open) }, props.children);
};
window.MobileTabs = function (props) {
  LOG.tabs.push(props.active);
  window.__selectTab = props.onSelect;
  var tab = props.tabs.filter(function (t) { return t.id === props.active; })[0];
  return React.createElement("div", { "data-testid": "tabs:" + props.active }, tab.content);
};
window.location = { reload: function () { LOG.reloads += 1; } };
var STORE = {};
var localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(STORE, k) ? STORE[k] : null; },
  setItem: function (k, v) { STORE[k] = String(v); },
  removeItem: function (k) { delete STORE[k]; },
};
window.matchMedia = function (q) { return { matches: q.indexOf("dark") >= 0, addEventListener: function () {} }; };
var ATTR = { "data-theme": "dark" };
var document = { documentElement: {
  getAttribute: function (k) { return ATTR[k] || null; },
  setAttribute: function (k, v) { ATTR[k] = String(v); },
} };
window.fetch = function (url, opts) {
  LOG.fetches.push({ url: url, method: opts && opts.method });
  return Promise.resolve({});
};
// A parent that keeps the pending link in state and clears it when the list reports it consumed it, as the shell does.
function PlatformHost(p) {
  var s = React.useState(p.initial);
  return React.createElement(NV_MobilePlatform, {
    pending: s[0],
    onPendingConsumed: function () { LOG.consumed += 1; s[1](null); },
    onNavChange: function (open) { LOG.navReports.push(open); },
  });
}
"""


def _platform_helpers() -> str:
    """The REAL helpers of ``nv-platform.jsx`` (``NV_emptyText``, ``NV_countText``, ``NV_nounOf``, and the navigation ones: ``NV_platNavAvailable``,
    ``NV_platNavLabel``, ``NV_platProviderTypes``, ``NV_PROVIDER_TYPE_LABELS``, ``NV_PLAT_CUSTOM_PAGES``), exported on ``window`` the way that file does.

    The mobile Platform list and the new-session picker read them from ``window`` (the console loads ``nv-platform.jsx`` before the shell). A stand-in
    that returned a fixed string would let a test pass against wording the console never says, so the harness takes the real functions: the slice runs
    from the first helper to the comment that opens the page table, the same boundary ``tests/ui/test_console_empty_lists.py`` slices.
    """
    src = PLATFORM.read_text(encoding="utf-8")
    start = src.index("function NV_emptyText(")
    end = src.index("// Per-entity page config.")
    exported = ("NV_emptyText", "NV_countText", "NV_platNavAvailable", "NV_platNavLabel", "NV_platProviderTypes", "NV_PROVIDER_TYPE_LABELS", "NV_PLAT_CUSTOM_PAGES")
    return src[start:end] + "\n" + "".join(f"window.{name} = {name};\n" for name in exported)


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(CHROME) + "\n" + transpile(SHELL)


@pytest.fixture
def app(code):
    """A fresh V8 context with the real shell loaded; closed after the test."""
    ctx = mini_react_context(code, "var window = globalThis;\n" + ATTENTION + "\n" + SHELL_URL + "\n" + _PRELUDE + _platform_helpers())

    class App:
        def js(self, expr: str):
            return ctx.eval(expr)

        def data(self, expr: str):
            return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))

        def mount(self, component: str, props: str = "{}") -> None:
            ctx.eval("MR.mount(" + component + ", " + props + ")")

        def has(self, testid: str) -> bool:
            return bool(ctx.eval("!!MR.find(" + json.dumps(testid) + ")"))

        def click(self, testid: str) -> None:
            ctx.eval("MR.click(" + json.dumps(testid) + ")")

        def attr(self, testid: str, name: str):
            return ctx.eval("MR.find(" + json.dumps(testid) + ").props[" + json.dumps(name) + "]")

        def texts(self) -> list[str]:
            return json.loads(ctx.eval("JSON.stringify(MR.texts())"))

        def navigate(self, name: str, nav: str | None = None, sub: str | None = None) -> None:
            """A goView call: the URL names the view, and the shell hands over a new view object."""
            ctx.eval("navigate(" + json.dumps(name) + ", " + json.dumps(nav) + ", " + json.dumps(sub) + "); MR.rerender();")

        def reparse(self) -> None:
            """A popstate or hashchange re-parsing whatever the URL says now into a fresh view object, with no goView."""
            ctx.eval("reparse(); MR.rerender();")

        def paste(self, name: str, nav: str | None = None, sub: str | None = None) -> None:
            """A link pasted into the address bar of an open console: the URL changes under the shell, which only sees a hashchange."""
            ctx.eval("URLVIEW = " + json.dumps({"name": name, "nav": nav, "sub": sub}) + "; reparse(); MR.rerender();")

        def select_tab(self, tab: str) -> None:
            ctx.eval("window.__selectTab(" + json.dumps(tab) + "); MR.rerender();")

    try:
        yield App()
    finally:
        ctx.close()


def _rows(app, role):
    return app.data("NV_mobileSettingsRows(" + json.dumps(role) + ")")


def test_the_settings_rows_follow_the_roles_the_desktop_menu_uses(app) -> None:
    assert [r["id"] for r in _rows(app, "admin")] == ["profile", "system"]
    assert [r["id"] for r in _rows(app, None)] == ["profile", "system"], "an install without roles is not restricted"
    assert [r["id"] for r in _rows(app, "restricted")] == ["profile"], (
        "Your profile is for every role; System settings stays hidden from a restricted user, as on desktop"
    )
    assert [r["label"] for r in _rows(app, "admin")] == ["Your profile", "System settings"]


def test_providers_is_not_a_settings_row_for_any_role(app) -> None:
    """Lead ruling (D4, D6): Providers is a section of the Platform list, and a Settings row that opened the providers overlay beside it was a duplicate entry, which
    the user dislikes. The overlay stays reachable by an ``overlay=providers:<class>`` link and from the desktop."""
    for role in ("admin", None, "restricted"):
        assert "providers" not in [r["id"] for r in _rows(app, role)], role


def test_your_profile_opens_system_profile(app) -> None:
    app.mount("NV_MobileMore", "{ pending: null, onPendingConsumed: function () {} }")
    app.click("nv-mob-setting:profile")
    assert app.data("LOG.views") == [["system", "profile"]]


# --- the Theme row (D3) ------------------------------------------------------------------------------------------------------


def test_the_phone_offers_light_dark_and_system_through_the_shared_helper(app) -> None:
    app.mount("NV_MobileProfileTheme")
    assert all(app.has(f"nv-mob-theme:{v}") for v in ("light", "dark", "system"))
    app.click("nv-mob-theme:system")
    assert app.data("LOG.tweaks")[-1] == ["theme", None]
    assert app.data('STORE["primer.theme.ana"]') == "system"
    assert app.attr("nv-mob-theme:system", "aria-pressed") == "true"


def test_the_phone_theme_row_is_labelled_and_each_choice_is_a_tap_sized_verb_button(app) -> None:
    app.mount("NV_MobileProfileTheme")
    assert "Theme" in app.texts()
    assert app.attr("nv-mob-theme-seg", "aria-labelledby") == "nv-mob-theme-label"
    for value in ("light", "dark", "system"):
        assert "touch-target" in app.attr(f"nv-mob-theme:{value}", "className").split()
        assert app.attr(f"nv-mob-theme:{value}", "data-verb") == f"theme.{value}"


def test_the_phone_presses_the_stored_choice_and_system_when_nothing_is_stored(app) -> None:
    app.mount("NV_MobileProfileTheme")
    assert [app.attr(f"nv-mob-theme:{v}", "aria-pressed") for v in ("light", "dark", "system")] == ["false", "false", "true"]
    app.js('STORE["primer.theme.ana"] = "dark"; MR.rerender();')
    assert [app.attr(f"nv-mob-theme:{v}", "aria-pressed") for v in ("light", "dark", "system")] == ["false", "true", "false"]


@pytest.mark.parametrize("os_theme", ["light", "dark"])
def test_choosing_system_on_the_phone_paints_the_os_theme_never_the_word_system(app, os_theme) -> None:
    app.js(f'window.matchMedia = function (q) {{ return {{ matches: q.indexOf("{os_theme}") >= 0, addEventListener: function () {{}} }}; }};')
    app.mount("NV_MobileProfileTheme")
    app.click("nv-mob-theme:system")
    assert app.data('ATTR["data-theme"]') == os_theme


def test_the_phone_restores_the_users_stored_theme(app) -> None:
    """The restore effect used to live in the desktop-only ``NV_ActivityBar``, so a phone never applied the theme the user had chosen on this browser."""
    app.js('STORE["primer.theme.ana"] = "light"; ATTR["data-theme"] = "dark";')
    app.mount("NV_MobileShell")
    assert app.data('ATTR["data-theme"]') == "light"
    assert app.data("LOG.tweaks")[-1] == ["theme", "light"]


def test_the_phone_leaves_the_theme_alone_for_a_user_who_never_chose(app) -> None:
    app.mount("NV_MobileShell")
    assert app.data("LOG.tweaks") == [] and app.data('ATTR["data-theme"]') == "dark"


# --- log out -----------------------------------------------------------------------------------------------------------------


def test_log_out_posts_to_the_logout_route_and_reloads(app) -> None:
    app.mount("NV_MobileProfileTheme")
    app.click("nv-mob-logout")
    assert app.data("LOG.fetches") == [{"url": "/v1/auth/logout", "method": "POST"}], (
        "a GET would not end the session (the route is POST-only) and would leave the phone signed in"
    )
    assert app.js("LOG.reloads") == 1, "the page reloads to land on the sign-in gate once the server has answered"


def test_log_out_reloads_even_when_the_server_cannot_be_reached(app) -> None:
    app.js("window.fetch = function (url, opts) { LOG.fetches.push({ url: url, method: opts && opts.method }); "
           "return Promise.reject(new Error('offline')); };")
    app.mount("NV_MobileProfileTheme")
    app.click("nv-mob-logout")
    assert len(app.data("LOG.fetches")) == 1
    assert app.js("LOG.reloads") == 1, "a dead network must not leave the button doing nothing"


# --- the platform list -------------------------------------------------------------------------------------------------------


def test_the_platform_list_reports_when_a_section_opens_and_closes(app) -> None:
    """The More tab hides its dashboard while a section is open, so the list has to say when one is."""
    app.mount("PlatformHost", '{ initial: { kind: "agents", id: null } }')
    assert app.data("LOG.navReports") == [False, True], "closed on mount, then open once the pending link opened the section"
    assert app.has("nv-mob-plat-page:agents")
    app.click("nv-mob-plat-back")
    assert app.data("LOG.navReports") == [False, True, False]
    assert app.has("nv-mob-plat-sections") and not app.has("nv-mob-plat-page:agents")


def test_opening_a_section_by_hand_reports_it_too(app) -> None:
    app.mount("PlatformHost", "{ initial: null }")
    assert app.has("nv-mob-plat-sections")
    app.click("nv-mob-plat-nav:toolsets")
    assert app.data("LOG.navReports") == [False, True]
    assert app.has("nv-mob-plat-page:toolsets")


def test_a_link_to_a_whole_section_is_consumed_as_soon_as_the_section_opens(app) -> None:
    """Left pending until the list had loaded, a Back tap in that first moment re-opened the section: the effect found a pending
    kind that was not the open nav and set it again (the phone journey caught it). The list is still loading here."""
    app.js("LOADING = true;")
    app.mount("PlatformHost", '{ initial: { kind: "agents", id: null } }')
    assert app.has("nv-mob-plat-page:agents")
    assert app.js("LOG.consumed") == 1, "consumed while the list is still loading, not after"
    app.click("nv-mob-plat-back")
    assert app.has("nv-mob-plat-sections"), "Back sticks: nothing is pending to open the section again"
    assert app.js("LOG.consumed") == 1


def test_a_link_to_a_row_waits_for_the_list_and_opens_its_fact_sheet(app) -> None:
    app.js('ITEMS = [{ id: "a1" }, { id: "a2" }];')
    app.mount("PlatformHost", '{ initial: { kind: "agents", id: "a2" } }')
    assert app.has("nv-mob-plat-page:agents")
    assert app.attr("sheet", "data-open") == "true"
    assert app.js("LOG.consumed") == 1


# --- the fact sheet's fire now and the platform list refetch -----------------------------------------------------------------

# The real NV_MobilePlatform + NV_MobileFactSheet with a counted refetch: what the fireNow harness in test_trigger_fired_outcomes.py
# cannot see is who passes the sheet its onChanged - a wrapper that passes its own would pass silently, and a deleted trigger would
# stay on the phone list for about 15 seconds. The envelope carries a request id the way the production middleware stamps it.
_GONE_TRIGGER = {
    "type": "/errors/not-found", "title": "Not Found", "status": 404, "detail": "tr-1",
    "extensions": {"code": "trigger_not_found", "message": "tr-1", "request_id": "req-0000000000aa"},
}
_SESSION_ENDED = {
    "type": "/errors/authentication-failed", "title": "Authentication Failed", "status": 401, "detail": "auth_required",
    "extensions": {"error": "auth_required", "request_id": "req-0000000000bb"},
}


def _cut(src: str, start: str, end: str) -> str:
    s = src.index(start)
    return src[s:src.index(end, s) + len(end)]


def _fire_now_wiring(app, envelope: dict) -> None:
    """Mount the real NV_MobilePlatform (not a host that passes the sheet its own refetch), open the trigger's fact sheet, and
    press Fire now with the real fireTrigger rejecting on a real ApiError."""
    trig = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")
    app.js((ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8"))
    app.js(_cut(trig, "var TR_REMEDIES = {", "\n};\n") + _cut(trig, "function TR_refusalText(", "\n}\n")
           + _cut(trig, "function TR_fireOutcome(", "\n}\n")
           + "\nwindow.TR_fireOutcome = TR_fireOutcome; window.TR_refusalText = TR_refusalText;")
    app.js(
        "var REFETCHES = 0; var TOASTS = [];"
        "NV_PLAT_GROUPS = [{ label: 'Automation', ids: ['triggers'] }];"
        "window.NV_PLAT_PAGES = { triggers: { title: 'Triggers', noun: ['trigger', 'triggers'], list: function () {},"
        " card: function (row) { return { id: row.id, name: row.id, sub: '', facts: [] }; } } };"
        "ITEMS = [{ id: 'tr-1', name: 'Nightly', slug: 'nightly' }];"
        "window.primerApi.useResource = function () { return { data: { items: ITEMS }, loading: false, error: null, degraded: false,"
        " refetch: function () { REFETCHES += 1; } }; };"
        "window.BottomSheet = function (props) { return React.createElement('div', { 'data-testid': 'sheet', 'data-open': String(!!props.open) },"
        " props.open ? [props.children, props.footer] : null); };"
        "__con = Object.assign({}, __con, { toast: function (msg, extra) { TOASTS.push({ text: String(msg), requestId: (extra && extra.requestId) || null }); } });"
        "SH_api.fireTrigger = function () { return Promise.reject(new window.primerApi.ApiError(" + json.dumps(envelope) + ")); };"
    )
    app.mount("NV_MobilePlatform", '{ pending: { kind: "triggers", id: null }, onPendingConsumed: function () {}, onNavChange: function () {} }')
    app.click("nv-mob-plat-row:tr-1")
    assert app.has("nv-mob-fact-fire"), "the fact sheet's Fire now button is rendered"
    app.click("nv-mob-fact-fire")
    app.js("void 0;")
    app.js("MR.rerender();")


def test_the_platform_hands_the_sheet_the_list_refetch_when_a_trigger_is_gone(app) -> None:
    _fire_now_wiring(app, _GONE_TRIGGER)
    assert app.data("TOASTS") == [
        {"text": "Nightly no longer exists; the list has been refreshed.", "requestId": "req-0000000000aa"}]
    assert not app.has("nv-mob-fact-fire"), "the sheet closed with the stale row"
    assert app.js("REFETCHES") == 1, "the phone list was refetched, once (the toast says it was)"


def test_the_platform_keeps_the_sheet_and_list_alone_for_other_refusals(app) -> None:
    _fire_now_wiring(app, _SESSION_ENDED)
    assert app.data("TOASTS") == [
        {"text": "Fire failed: Your session has ended; sign in again.", "requestId": "req-0000000000bb"}]
    assert app.has("nv-mob-fact-fire"), "the sheet stays on the live row"
    assert app.js("REFETCHES") == 0


# --- the fact sheet draws the card view model (seam I-8) ---------------------------------------------------------------------


def _open_sheet(app, card_js: str) -> None:
    """The agents section with one row whose card is ``card_js``, opened on that row's fact sheet the way a link to the row opens it."""
    app.js("window.NV_PLAT_PAGES.agents.card = " + card_js + '; ITEMS = [{ id: "a1" }];')
    app.mount("PlatformHost", '{ initial: { kind: "agents", id: "a1" } }')
    assert app.attr("sheet", "data-open") == "true"


def test_the_platform_list_hands_the_console_to_card(app) -> None:
    app.js("var SEEN = [];")
    _open_sheet(app, 'function (row, con) { SEEN.push(con); return { id: row.id, name: row.id, sub: "", facts: [] }; }')
    assert app.js("SEEN.length > 0 && SEEN.every(function (c) { return c === __con; })"), "card(row, con) gets the console on the phone too"


def test_the_fact_sheet_shows_badge_and_status(app) -> None:
    _open_sheet(app, 'function (row) { return { id: row.id, name: row.id, sub: "a toolset", badge: "built in",'
                     ' status: { label: "available", tone: "ok" }, facts: [] }; }')
    texts = app.data("MR.texts()")
    assert "built in" in texts and "available" in texts
    assert app.js('MR.find("nv-mob-fact-body") !== null')
    status = app.data('MR.subtree("nv-mob-fact-body").filter(function (e) { return /nv-mob-fact-status/.test(e.className); })')
    assert len(status) == 1 and status[0]["type"] == "span", "a status with no action is text"


def test_a_clickable_status_on_the_fact_sheet_closes_the_sheet_and_runs_its_action(app) -> None:
    app.js("var RAN = 0;")
    _open_sheet(app, 'function (row) { return { id: row.id, name: row.id, sub: "", facts: [],'
                     ' status: { label: "Internal search: off", tone: "warn", testid: "chip-a1", onClick: function () { RAN += 1; } } }; }')
    chip = app.js('MR.find("chip-a1").type')
    assert chip == "button"
    assert "touch-target" in app.js('MR.find("chip-a1").props.className').split()
    assert app.js('MR.find("chip-a1").props.children') == "Internal search: off"
    app.click("chip-a1")
    assert app.attr("sheet", "data-open") == "false", "the sheet closes first"
    assert app.js("RAN") == 1, "then the action runs, once"


def test_the_phone_list_uses_the_empty_text_and_action(app) -> None:
    """Seam I-9 on the phone: a section with no rows says the page's own words and offers its way out as a tap-sized button."""
    app.js("var GOT = []; window.NV_PLAT_PAGES.agents.noun = ['agent', 'agents'];"
           "window.NV_PLAT_PAGES.agents.emptyText = 'No LLM profiles yet. Register an LLM provider to import its models.';"
           "window.NV_PLAT_PAGES.agents.emptyAction = { label: 'Open Providers › LLM', run: function (con) { GOT.push(con === __con); } };"
           "ITEMS = [];")
    app.mount("PlatformHost", '{ initial: { kind: "agents", id: null } }')
    assert app.has("nv-mob-plat-empty:agents")
    texts = app.data("MR.texts()")
    assert "No LLM profiles yet. Register an LLM provider to import its models." in texts and "No agents yet." not in texts
    assert app.js('MR.find("nv-mob-plat-empty-action").type') == "button"
    assert "touch-target" in app.js('MR.find("nv-mob-plat-empty-action").props.className').split()
    assert app.js('MR.find("nv-mob-plat-empty-action").props.children') == "Open Providers › LLM"
    app.click("nv-mob-plat-empty-action")
    assert app.data("GOT") == [True], "the action ran once, with the console"


def test_a_phone_section_without_hooks_keeps_the_generic_empty_text(app) -> None:
    app.js("window.NV_PLAT_PAGES.agents.noun = ['agent', 'agents']; ITEMS = [];")
    app.mount("PlatformHost", '{ initial: { kind: "agents", id: null } }')
    assert "No agents yet." in app.data("MR.texts()")
    assert not app.has("nv-mob-plat-empty-action")


def test_a_plain_string_fact_is_one_line(app) -> None:
    _open_sheet(app, 'function (row) { return { id: row.id, name: row.id, sub: "", facts: ["vLLM · 1 model", null, ["context", "8192"]] }; }')
    texts = app.data("MR.texts()")
    assert "vLLM · 1 model" in texts, "the string is drawn whole, not as a key and a value cut from its first letters"
    assert "v" not in texts and "L" not in texts
    assert "context" in texts and "8192" in texts


# --- the Platform sections are the available navs: Providers and its types, custom pages (Platform revamp 01, Task 8; seam I-2 on phones) -------

TYPES = ["llm", "embedding", "ssp", "cross_encoder", "workspace", "web_search", "channel", "artifact_storage", "tts", "stt"]
LABELS = ["LLM", "Embedding", "Semantic Search Storage", "Cross Encoding", "Workspace", "Web Search", "Channels", "Artifact Store", "TTS", "ASR"]
LOAD_WEB = "window.WEB_SettingsPage = STAND_IN('web-stand-in');"
LOAD_BOTH = LOAD_WEB + " window.WT_TemplatesPage = STAND_IN('templates-stand-in');"
INTELLIGENCE = 'NV_PLAT_GROUPS = [{ label: "Intelligence", ids: ["providers", "agents"] }];'


def _ids(app, prefix: str) -> list[str]:
    """The ids after ``prefix`` of every element whose test id starts with it, in the order they are drawn."""
    return app.data(f'MR.findAll({json.dumps(prefix)}).map(function (e) {{ return e.props["data-testid"].slice({len(prefix)}); }})')


def _label(app, testid: str) -> str:
    return app.js(f"TXT(MR.find({json.dumps(testid)}).children)")


def _title(app) -> str:
    return app.js('TXT(MR.find("nv-mob-plat-title").children)')


def _catalog(app) -> dict:
    return app.data("CATALOG.seen[CATALOG.seen.length - 1]")


def _open_providers(app, pending: str = "null") -> None:
    app.js(INTELLIGENCE)
    app.mount("PlatformHost", "{ initial: " + pending + " }")


def test_providers_opens_a_second_level_list_of_types(app) -> None:
    """Design 4.3: the phone offers the same entries as the sidebar; the provider types are a second-level list under Providers."""
    _open_providers(app)
    assert app.has("nv-mob-plat-nav:providers") and app.has("nv-mob-plat-nav:agents")
    assert _label(app, "nv-mob-plat-nav:providers") == "Providers"
    app.click("nv-mob-plat-nav:providers")
    assert app.has("nv-mob-plat-page:providers") and not app.has("nv-mob-plat-sections")
    assert _ids(app, "nv-mob-plat-sub:") == ["all"] + TYPES, "All providers first, then the types in the sidebar's order"
    labels = app.data('MR.findAll("nv-mob-plat-sub:").map(function (e) { return TXT(e.children); })')
    assert labels == ["All providers"] + LABELS
    assert not app.has("catalog-stand-in"), "the list of types is shown before any catalogue"


def test_the_type_list_follows_the_grammar_not_a_list_of_its_own(app) -> None:
    app.js("window.SH_VIEW_SUBS = {};")
    _open_providers(app)
    app.click("nv-mob-plat-nav:providers")
    assert _ids(app, "nv-mob-plat-sub:") == ["all"], "no types in the grammar, no types on the phone"


def test_all_providers_shows_the_catalog_at_all(app) -> None:
    _open_providers(app)
    app.click("nv-mob-plat-nav:providers")
    app.click("nv-mob-plat-sub:all")
    assert _catalog(app) == {"initialClass": "all", "initialInstanceId": "null"}
    assert _title(app) == "All providers"


def test_a_type_shows_the_catalog_at_that_type_and_back_returns_to_the_types(app) -> None:
    _open_providers(app)
    app.click("nv-mob-plat-nav:providers")
    app.click("nv-mob-plat-sub:tts")
    assert _catalog(app) == {"initialClass": "tts", "initialInstanceId": "null"}, "the catalogue opens at the type, with no instance open"
    assert app.attr("catalog-stand-in", "data-class") == "tts" and _title(app) == "TTS"
    assert app.has("nv-mob-plat-page:providers") and not app.has("nv-mob-plat-sub:tts"), "the catalogue replaces the list"
    app.click("nv-mob-plat-back")
    assert app.has("nv-mob-plat-sub:all") and not app.has("catalog-stand-in"), "Back from a type is the list of types"
    assert app.data("[CATALOG.mounts, CATALOG.unmounts]") == [1, 1]
    app.click("nv-mob-plat-back")
    assert app.has("nv-mob-plat-sections") and not app.has("nv-mob-plat-page:providers"), "and Back from the list is the sections"


def test_the_providers_screens_count_as_an_open_section(app) -> None:
    """The More tab hides its dashboard while a section is open, and the type list and the catalogue are both the Providers section."""
    _open_providers(app)
    app.click("nv-mob-plat-nav:providers")
    app.click("nv-mob-plat-sub:llm")
    app.click("nv-mob-plat-back")
    app.click("nv-mob-plat-back")
    assert app.data("LOG.navReports") == [False, True, False], "open from the Providers row until Back reaches the sections, and no flicker in between"


def test_the_more_tab_makes_room_for_the_providers_screens(app) -> None:
    app.js(INTELLIGENCE)
    app.mount("NV_MobileMore", "{ pending: null, onPendingConsumed: function () {} }")
    app.click("nv-mob-plat-nav:providers")
    assert app.attr("nv-mobile-panel:more", "data-section-open") == "true" and not app.has("nv-mob-profile")
    app.click("nv-mob-plat-sub:ssp")
    assert app.attr("nv-mobile-panel:more", "data-section-open") == "true" and app.has("catalog-stand-in")


def test_a_chip_the_catalog_moves_itself_to_names_the_title_and_keeps_it_mounted(app) -> None:
    """The catalogue keeps its own type chips until Plan 02 replaces its body. A chip is the catalogue's own move: the title follows it, and the catalogue is
    not remounted (a card click selects its class AND opens its edit form in one call; a remount would close the form)."""
    _open_providers(app)
    app.click("nv-mob-plat-nav:providers")
    app.click("nv-mob-plat-sub:tts")
    app.js('CATALOG.props.onNavigate({ kind: "provider-class", classKey: "embedding" }); MR.rerender();')
    assert _title(app) == "Embedding"
    assert app.data("[CATALOG.mounts, CATALOG.unmounts]") == [1, 0]
    app.js('CATALOG.props.onNavigate({ kind: "provider-class", classKey: "web_fetch" }); CATALOG.props.onNavigate({ kind: "provider-instance", classKey: "llm", id: "p1" });'
           ' CATALOG.props.onNavigate(null); MR.rerender();')
    assert _title(app) == "Embedding", "a class with no entry in the list, an instance and nothing at all leave the title alone"
    assert app.js("LOG.views.length") == 0, "the phone's catalogue moves no address: its Back button is the way out"


def test_picking_a_type_again_starts_its_catalog_over(app) -> None:
    _open_providers(app)
    app.click("nv-mob-plat-nav:providers")
    app.click("nv-mob-plat-sub:tts")
    app.click("nv-mob-plat-back")
    app.click("nv-mob-plat-sub:tts")
    assert app.data("[CATALOG.mounts, CATALOG.unmounts]") == [2, 1], "a new instance, with no instance open in it"


def test_a_custom_page_is_listed_and_mounts_only_while_its_component_is_loaded(app) -> None:
    """Seam I-2 on the phone: a custom nav is a section while its component is a function on window, and it mounts with no props under Back and its label."""
    app.js('NV_PLAT_GROUPS = [{ label: "Workbench", ids: ["toolsets", "templates", "web"] }];')
    app.mount("PlatformHost", "{ initial: null }")
    assert _ids(app, "nv-mob-plat-nav:") == ["toolsets"], "no placeholder row until the component is loaded"
    app.js(LOAD_WEB + " MR.rerender();")
    assert _ids(app, "nv-mob-plat-nav:") == ["toolsets", "web"], "each row follows its own component"
    assert _label(app, "nv-mob-plat-nav:web") == "Web"
    app.js("window.WT_TemplatesPage = STAND_IN('templates-stand-in'); MR.rerender();")
    assert _ids(app, "nv-mob-plat-nav:") == ["toolsets", "templates", "web"], "in the order of the group"
    assert _label(app, "nv-mob-plat-nav:templates") == "Workspace templates"

    app.click("nv-mob-plat-nav:web")
    assert app.has("nv-mob-plat-page:web") and not app.has("nv-mob-plat-sections")
    assert "web-stand-in" in app.data('MR.subtree("nv-mob-plat-page:web").map(function (e) { return e.testid; })')
    assert app.data("STAND_IN_PROPS.length > 0 && STAND_IN_PROPS.every(function (keys) { return keys.length === 0; })"), (
        "the component reads the console itself and is given no props"
    )
    assert _title(app) == "Web" and app.has("nv-mob-plat-back")
    assert app.data("LOG.navReports") == [False, True]
    app.click("nv-mob-plat-back")
    assert app.has("nv-mob-plat-sections") and not app.has("nv-mob-plat-page:web")
    assert app.data("LOG.navReports") == [False, True, False]
    app.click("nv-mob-plat-nav:templates")
    assert "templates-stand-in" in app.data('MR.subtree("nv-mob-plat-page:templates").map(function (e) { return e.testid; })')
    assert _title(app) == "Workspace templates"


def test_a_custom_page_whose_component_goes_away_while_open_shows_the_section_list(app) -> None:
    """Never a blank screen: a nav with no page behind it is the list of sections."""
    app.js('NV_PLAT_GROUPS = [{ label: "Workbench", ids: ["toolsets", "web"] }];' + LOAD_WEB)
    app.mount("PlatformHost", "{ initial: null }")
    app.click("nv-mob-plat-nav:web")
    assert app.has("nv-mob-plat-page:web")
    app.js("delete window.WEB_SettingsPage; MR.rerender();")
    assert app.has("nv-mob-plat-sections") and not app.has("nv-mob-plat-page:web") and not app.has("nv-mob-plat-nav:web")
    assert app.data("LOG.navReports")[-1] is False


def test_a_provider_type_link_opens_the_providers_section_at_that_type(app) -> None:
    """``?view=platform:providers:tts`` (or any caller of ``goView("platform", "providers", "tts")``): the More tab, the catalogue at the type, and the link is consumed."""
    app.js('navigate("platform", "providers", "tts");')
    app.mount("NV_MobileShell")
    assert app.has("tabs:more"), "the shell used to stay on Inbox"
    assert _catalog(app) == {"initialClass": "tts", "initialInstanceId": "null"}
    assert app.has("nv-mob-plat-page:providers") and not app.has("nv-mob-profile"), "with the section filling the tab"
    assert app.js("LOG.cleared") == 1, "consumed like any handled platform link"
    app.click("nv-mob-plat-back")
    assert app.has("nv-mob-plat-sub:tts"), "Back from the type is the list of types, and it stays there"
    app.reparse()
    assert app.has("nv-mob-plat-sub:tts") and not app.has("catalog-stand-in"), "a popstate does not reopen the type"


def test_a_link_to_providers_without_a_type_opens_the_list_of_types(app) -> None:
    app.js('navigate("platform", "providers");')
    app.mount("NV_MobileShell")
    assert app.has("tabs:more") and _ids(app, "nv-mob-plat-sub:") == ["all"] + TYPES
    assert not app.has("catalog-stand-in")
    assert app.js("LOG.cleared") == 1


def test_a_pending_type_the_grammar_does_not_list_opens_the_list_of_types(app) -> None:
    """The grammar drops such a type before the shell sees it; a hand-built link must still not mount the catalogue at a class that is not there."""
    _open_providers(app, '{ kind: "providers", id: null, sub: "bogus" }')
    assert app.has("nv-mob-plat-sub:all") and not app.has("catalog-stand-in")
    assert app.js("LOG.consumed") == 1


def test_a_type_belongs_to_providers_alone(app) -> None:
    app.mount("PlatformHost", '{ initial: { kind: "agents", id: null, sub: "tts" } }')
    assert app.has("nv-mob-plat-page:agents") and not app.has("catalog-stand-in")


def test_a_link_to_another_type_replaces_the_open_catalog_and_the_same_link_starts_it_over(app) -> None:
    app.js('navigate("platform", "providers", "tts");')
    app.mount("NV_MobileShell")
    assert app.data("[CATALOG.mounts, CATALOG.unmounts]") == [1, 0]
    app.navigate("platform", "providers", "llm")
    assert _catalog(app)["initialClass"] == "llm" and _title(app) == "LLM"
    assert app.data("[CATALOG.mounts, CATALOG.unmounts]") == [2, 1], "a new instance, so an instance open under the old type is not carried over"
    app.navigate("platform", "providers", "llm")
    assert app.data("[CATALOG.mounts, CATALOG.unmounts]") == [3, 2], "the same link again is a way back to the type's cards"
    app.navigate("platform", "agents")
    assert app.has("nv-mob-plat-page:agents") and not app.has("catalog-stand-in")
    app.navigate("platform", "providers", "ssp")
    assert _catalog(app)["initialClass"] == "ssp"


def test_a_custom_page_link_opens_the_page_while_its_component_is_loaded_and_the_sections_when_not(app) -> None:
    """Review Focus 5 on the phone: a link to a page that is not there is the section list, not a blank page or an error."""
    app.js('NV_PLAT_GROUPS = [{ label: "Workbench", ids: ["toolsets", "web"] }]; navigate("platform", "web");')
    app.mount("NV_MobileShell")
    assert app.has("tabs:more") and app.has("nv-mob-plat-sections") and not app.has("nv-mob-plat-page:web")
    assert app.js("LOG.cleared") == 1
    app.js(LOAD_WEB)
    app.navigate("platform", "web")
    assert app.has("nv-mob-plat-page:web") and "web-stand-in" in app.data('MR.subtree("nv-mob-plat-page:web").map(function (e) { return e.testid; })')


def _real_pages(app) -> None:
    """The REAL sidebar groups and page table (titles included) of ``nv-platform.jsx``: the groups literal, then the plain-JS region (JSX-free, it ends where the page size begins)."""
    src = PLATFORM.read_text(encoding="utf-8")
    groups = src.index("var NV_PLAT_GROUPS = [")
    app.js(src[groups:src.index("\n];\n", groups) + len("\n];\n")])
    app.js(src[src.index("function NV_fact("):src.index("var NV_PLAT_PAGE_SIZE")])


def test_the_sections_are_the_available_navs_in_the_order_of_the_grammar(app) -> None:
    """Design 4.3: the phone's list is the sidebar's. Providers has no page-table entry (its page is the catalogue) and a custom nav has none either, and
    all three are sections; before this the phone offered only the navs that had a page-table entry."""
    _real_pages(app)
    app.mount("PlatformHost", "{ initial: null }")
    shown = _ids(app, "nv-mob-plat-nav:")
    assert shown == ["providers", "profiles", "agents", "graphs", "toolsets", "tools", "collections", "triggers", "channels", "harnesses", "services", "approvals"]
    app.js(LOAD_BOTH + " MR.rerender();")
    every = app.data("SH_VIEWS.platform")
    assert _ids(app, "nv-mob-plat-nav:") == every, "with both custom pages loaded, every nav of the grammar is a section, in the grammar's order"
    assert app.data("NV_PLAT_GROUPS.map(function (g) { return g.label; })") == ["Intelligence", "Workbench", "Automation", "Governance"]
    assert [t for t in app.texts() if t in ("Intelligence", "Workbench", "Automation", "Governance")] == ["Intelligence", "Workbench", "Automation", "Governance"]


def test_the_workspaces_section_is_gone_and_profiles_reads_llm_profiles(app) -> None:
    """Real titles: Workspaces left the Platform view (design 14.1), and the profiles page is LLM Profiles (A8), on the phone as on the desktop."""
    _real_pages(app)
    app.mount("PlatformHost", "{ initial: null }")
    assert not app.has("nv-mob-plat-nav:workspaces")
    assert _label(app, "nv-mob-plat-nav:profiles") == "LLM Profiles"
    assert _label(app, "nv-mob-plat-nav:channels") == "Channels" and _label(app, "nv-mob-plat-nav:approvals") == "Approval policies"
    assert not any(chr(0x2014) in text for text in app.texts()), "no long dash in the phone's list"


# --- the More tab ------------------------------------------------------------------------------------------------------------


def test_the_dashboard_shows_until_a_section_opens_and_then_makes_room(app) -> None:
    app.mount("NV_MobileMore", "{ pending: null, onPendingConsumed: function () {} }")
    assert app.attr("nv-mobile-panel:more", "data-section-open") == "false"
    for testid in ("nv-mob-profile", "nv-mob-settings", "health-cards", "nv-mob-plat-sections"):
        assert app.has(testid), testid + " belongs on the More tab while no section is open"
    app.click("nv-mob-plat-nav:agents")
    assert app.attr("nv-mobile-panel:more", "data-section-open") == "true"
    for testid in ("nv-mob-profile", "nv-mob-settings", "health-cards"):
        assert not app.has(testid), testid + " must not push an open section 1300px down the screen"
    assert app.has("nv-mob-plat-page:agents")
    app.click("nv-mob-plat-back")
    assert app.attr("nv-mobile-panel:more", "data-section-open") == "false"
    assert app.has("nv-mob-profile"), "closing the section brings the dashboard back"


def test_more_offers_system_settings_and_log_out_and_reaches_providers_only_as_a_platform_section(app) -> None:
    """Lead ruling (D4, D6): one entry to Providers on the More tab, the Platform section; the Settings row that opened the providers overlay is gone."""
    app.js(INTELLIGENCE)
    app.mount("NV_MobileMore", "{ pending: null, onPendingConsumed: function () {} }")
    assert app.has("nv-mob-setting:system") and app.has("nv-mob-logout")
    assert not app.has("nv-mob-setting:providers")
    assert _ids(app, "nv-mob-setting:") == ["profile", "system"]
    to_providers = app.data('MR.findAll("nv-mob-").map(function (e) { return e.props["data-testid"]; }).filter(function (t) { return /providers/.test(t); })')
    assert to_providers == ["nv-mob-plat-nav:providers"], "the only entry that names Providers"
    app.click("nv-mob-setting:system")
    assert app.data("LOG.views") == [["system", None]]
    app.click("nv-mob-plat-nav:providers")
    assert app.has("nv-mob-plat-page:providers") and app.has("nv-mob-plat-sub:all"), "it opens the phone's own list of provider types"
    assert app.data("LOG.overlays") == [], "and no providers overlay"


def test_a_restricted_user_is_not_offered_system_settings(app) -> None:
    app.js('__con = Object.assign({}, __con, { role: "restricted" });')
    app.mount("NV_MobileMore", "{ pending: null, onPendingConsumed: function () {} }")
    assert _ids(app, "nv-mob-setting:") == ["profile"], "Your profile only: no System settings, and Providers is a Platform section"


# --- views and overlays arriving at the shell --------------------------------------------------------------------------------


def test_a_platform_view_link_lands_on_the_more_tab_with_that_section_open(app) -> None:
    app.js('navigate("platform", "agents");')
    app.mount("NV_MobileShell")
    assert app.has("tabs:more"), "the shell used to stay on Inbox"
    assert app.has("nv-mob-plat-page:agents"), "and the named section is the one that opens"
    assert not app.has("nv-mob-profile"), "with the section filling the tab"


def test_a_platform_view_without_a_section_lands_on_the_more_tab_at_the_section_list(app) -> None:
    app.js('navigate("platform", null);')
    app.mount("NV_MobileShell")
    assert app.has("tabs:more")
    assert app.has("nv-mob-plat-sections") and not app.has("nv-mob-plat-page:agents")


def test_a_view_that_is_not_the_platform_leaves_the_tab_alone(app) -> None:
    app.js('navigate("studio", null);')
    app.mount("NV_MobileShell")
    assert app.has("tabs:inbox")


def test_running_the_same_platform_link_again_after_back_opens_the_section_again(app) -> None:
    """On a phone at ``?view=platform:agents``, Back leaves the section and the URL still names it. Running the Platform verb for
    the same section again produces the same name and nav, so an effect that watched only those values saw no change and nothing
    happened. The shell's goView hands over a new view object on every call; that is what must reopen the section."""
    app.js('navigate("platform", "agents");')
    app.mount("NV_MobileShell")
    assert app.has("nv-mob-plat-page:agents")
    app.click("nv-mob-plat-back")
    assert app.has("nv-mob-plat-sections") and not app.has("nv-mob-plat-page:agents")

    app.js("MR.rerender();")
    assert app.has("nv-mob-plat-sections"), "re-rendering with the same view is not a navigation: Back must stick"

    app.navigate("platform", "agents")
    assert app.has("nv-mob-plat-page:agents"), "the same link run again opens the section again"
    app.click("nv-mob-plat-back")
    app.navigate("platform", "agents")
    assert app.has("nv-mob-plat-page:agents"), "and every time after that"


def test_a_handled_platform_link_is_consumed_so_nothing_can_replay_it(app) -> None:
    """On a phone nothing cleared ``view=platform:<x>`` from the URL, and nv-shell's onNav re-parses the URL into a fresh view object
    on EVERY hashchange or popstate, so a later Android back gesture re-opened the section behind whatever the user had moved on to.
    Once the shell has acted on the link it drops the view from the URL (a replace, no history entry)."""
    app.js('navigate("platform", "agents");')
    app.mount("NV_MobileShell")
    assert app.has("nv-mob-plat-page:agents")
    assert app.js("LOG.cleared") == 1 and app.js("URLVIEW.name") == "studio", "the URL no longer names the view"


def test_a_popstate_that_reparses_the_url_after_back_does_not_reopen_the_section(app) -> None:
    app.js('navigate("platform", "agents");')
    app.mount("NV_MobileShell")
    app.click("nv-mob-plat-back")
    assert app.has("nv-mob-plat-sections")
    app.reparse()
    assert app.has("nv-mob-plat-sections") and not app.has("nv-mob-plat-page:agents"), "the section must stay closed"
    app.reparse()
    assert app.has("nv-mob-plat-sections"), "and again, however many times the URL is re-parsed"


def test_a_popstate_does_not_pull_the_phone_back_to_the_more_tab(app) -> None:
    """Back, Inbox, open a session, back gesture: the tab the user is on is theirs; a re-parsed URL does not move it."""
    app.js('navigate("platform", "agents");')
    app.mount("NV_MobileShell")
    app.click("nv-mob-plat-back")
    app.select_tab("inbox")
    assert app.has("tabs:inbox")
    app.reparse()
    assert app.has("tabs:inbox") and not app.has("nv-mob-plat-page:agents")


def test_a_link_run_through_goview_still_pulls_the_phone_to_the_section_from_another_tab(app) -> None:
    app.js('navigate("platform", "agents");')
    app.mount("NV_MobileShell")
    app.click("nv-mob-plat-back")
    app.select_tab("inbox")
    app.navigate("platform", "agents")
    assert app.has("tabs:more") and app.has("nv-mob-plat-page:agents")
    assert app.js("LOG.cleared") == 2, "consumed again"


def test_a_link_pasted_into_an_open_console_opens_the_section(app) -> None:
    """The shell only sees a hashchange here, exactly as it does for a back gesture: the two cannot be told apart by the event, which
    is why the link is consumed once handled instead of the effect trying to guess which events are navigations."""
    app.mount("NV_MobileShell")
    assert app.has("tabs:inbox")
    app.paste("platform", "agents")
    assert app.has("tabs:more") and app.has("nv-mob-plat-page:agents")
    app.click("nv-mob-plat-back")
    app.paste("platform", "agents")
    assert app.has("nv-mob-plat-page:agents"), "a second paste of the same link opens it again"


def test_a_link_to_the_whole_platform_is_consumed_too(app) -> None:
    app.js('navigate("platform", null);')
    app.mount("NV_MobileShell")
    assert app.has("nv-mob-plat-sections")
    assert app.js("LOG.cleared") == 1


def test_a_system_view_is_not_consumed_because_it_is_the_screen_itself(app) -> None:
    app.js('navigate("system", null);')
    app.mount("NV_MobileShell")
    assert app.has("nv-mob-system-screen")
    assert app.js("LOG.cleared") == 0 and app.js("URLVIEW.name") == "system"


def test_a_platform_link_for_another_section_switches_to_it(app) -> None:
    app.js('navigate("platform", "agents");')
    app.mount("NV_MobileShell")
    app.navigate("platform", "toolsets")
    assert app.has("nv-mob-plat-page:toolsets") and not app.has("nv-mob-plat-page:agents")


def test_an_overlay_naming_a_platform_page_opens_its_row_on_the_more_tab(app) -> None:
    app.js('ITEMS = [{ id: "a1" }]; __con = Object.assign({}, __con, { overlay: { name: "agents", id: "a1" } });')
    app.mount("NV_MobileShell")
    assert app.has("tabs:more")
    assert app.has("nv-mob-plat-page:agents") and app.attr("sheet", "data-open") == "true"
    assert app.js("LOG.closedOverlays") >= 1, "the desktop overlay is closed in favour of the fact sheet"


def test_a_system_view_opens_full_screen_with_a_way_back(app) -> None:
    app.js('navigate("system", null);')
    app.mount("NV_MobileShell")
    assert app.has("nv-mob-system-screen")
    assert not app.has("tabs:more") and not app.has("tabs:inbox"), "no tab bar under a takeover screen"
    app.click("nv-mob-system-back")
    assert app.data("LOG.views") == [["studio", None]]
