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

# What the file's globals need to exist; each is the smallest stand-in that lets the real component render.
_PRELUDE = r"""
var LOG = { fetches: [], reloads: 0, overlays: [], views: [], closedOverlays: 0, navReports: [], consumed: 0, tabs: [], cleared: 0 };
var ITEMS = [];
var LOADING = false;
var __con = {
  username: "ana", role: "admin", wid: "w1", doc: null, overlay: null, view: { name: "studio", nav: null },
  openOverlay: function (name) { LOG.overlays.push(name); },
  clearView: function () { LOG.cleared += 1; URLVIEW = { name: "studio", nav: null }; __con = Object.assign({}, __con, { view: fresh() }); },
  closeOverlay: function () { LOG.closedOverlays += 1; },
  goView: function (name, nav) { LOG.views.push([name, nav || null]); },
  toast: function () {}, bump: function () {},
};
function NV_useConsole() { return __con; }
// The view as the URL names it, and what the shell does with it. goView writes the URL and hands over a NEW view object; a hashchange or
// popstate (nv-shell's onNav) re-parses the URL into another FRESH object without any goView; clearView drops the view from state, and
// so from the URL, as a replace (no history entry).
var URLVIEW = { name: "studio", nav: null };
function fresh() { return { name: URLVIEW.name, nav: URLVIEW.nav }; }
function navigate(name, nav) { URLVIEW = { name: name, nav: nav || null }; __con = Object.assign({}, __con, { view: fresh() }); }
function reparse() { __con = Object.assign({}, __con, { view: fresh() }); }
var NV_HealthCards = function () { return React.createElement("div", { "data-testid": "health-cards" }); };
var NV_PLAT_GROUPS = [{ label: "Build", ids: ["agents", "toolsets"] }];
window.NV_PLAT_PAGES = {
  agents: { title: "Agents", list: function () {}, card: function (row) { return { name: "agent " + row.id }; } },
  toolsets: { title: "Toolsets", list: function () {}, card: function (row) { return { name: "toolset " + row.id }; } },
};
window.primerApi = {
  apiFetch: function () {},
  useResource: function () {
    return { data: { items: ITEMS }, loading: LOADING, error: null, degraded: false, refetch: function () {} };
  },
};
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
var document = { documentElement: { getAttribute: function () { return "dark"; }, setAttribute: function () {} } };
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


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(SHELL)


@pytest.fixture
def app(code):
    """A fresh V8 context with the real shell loaded; closed after the test."""
    ctx = mini_react_context(code, _PRELUDE)

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

        def navigate(self, name: str, nav: str | None = None) -> None:
            """A goView call: the URL names the view, and the shell hands over a new view object."""
            ctx.eval("navigate(" + json.dumps(name) + ", " + json.dumps(nav) + "); MR.rerender();")

        def reparse(self) -> None:
            """A popstate or hashchange re-parsing whatever the URL says now into a fresh view object, with no goView."""
            ctx.eval("reparse(); MR.rerender();")

        def paste(self, name: str, nav: str | None = None) -> None:
            """A link pasted into the address bar of an open console: the URL changes under the shell, which only sees a hashchange."""
            ctx.eval("URLVIEW = " + json.dumps({"name": name, "nav": nav}) + "; reparse(); MR.rerender();")

        def select_tab(self, tab: str) -> None:
            ctx.eval("window.__selectTab(" + json.dumps(tab) + "); MR.rerender();")

    try:
        yield App()
    finally:
        ctx.close()


def _rows(app, role):
    return app.data("NV_mobileSettingsRows(" + json.dumps(role) + ")")


def test_the_settings_rows_follow_the_roles_the_desktop_menu_uses(app) -> None:
    assert [r["id"] for r in _rows(app, "admin")] == ["providers", "system"]
    assert [r["id"] for r in _rows(app, None)] == ["providers", "system"], "an install without roles is not restricted"
    assert [r["id"] for r in _rows(app, "restricted")] == ["providers"], (
        "System settings stays hidden from a restricted user, as on desktop"
    )
    assert [r["label"] for r in _rows(app, "admin")] == ["Providers", "System settings"]


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


def test_more_offers_providers_system_settings_and_log_out(app) -> None:
    app.mount("NV_MobileMore", "{ pending: null, onPendingConsumed: function () {} }")
    assert app.has("nv-mob-setting:providers") and app.has("nv-mob-setting:system") and app.has("nv-mob-logout")
    app.click("nv-mob-setting:providers")
    app.click("nv-mob-setting:system")
    assert app.data("LOG.overlays") == ["providers"]
    assert app.data("LOG.views") == [["system", None]]


def test_a_restricted_user_is_not_offered_system_settings(app) -> None:
    app.js('__con = Object.assign({}, __con, { role: "restricted" });')
    app.mount("NV_MobileMore", "{ pending: null, onPendingConsumed: function () {} }")
    assert app.has("nv-mob-setting:providers") and not app.has("nv-mob-setting:system")


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
