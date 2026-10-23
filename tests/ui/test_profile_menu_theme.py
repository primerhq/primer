"""The profile menu's Theme row (Light, Dark, System) and its Your profile row (platform revamp D3, 2026-10-18).

Before: a small unlabelled "Dark | Light" pill that shrink-wrapped (the later ``.nv-seg`` rule is ``inline-flex``), no way to follow the operating system after
the first visit, and no way to reach your own profile from the menu. Now the menu has a labelled Theme row and a Your profile row for every role, and the same
choices are palette verbs.

These run the real ``nv-chrome.jsx`` (transpiled the way the server bundles it) with the real ``ui/foundation/tweaks.js`` in V8 on the small hook runtime in
``tests/ui/_mini_react.py``, so what is asserted is what the helpers WROTE (``data-theme``, the per-user key, the persisted tweak) and what the menu rendered.
The failure the design most needs ruled out is ``data-theme="system"``: no token block defines it, so the console would lose every colour (Review Focus 1 of
the plan). ``data-theme`` is always ``light`` or ``dark``; System is stored as the word ``system`` per user and as ``null`` in the tweak, which the inline
bootstrap of ``ui/index.html`` and the ``matchMedia`` listener of ``tweaks.js`` already read as "follow the OS".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHROME_PATH = UI / "components" / "console" / "nv-chrome.jsx"
SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
TWEAKS = (UI / "foundation" / "tweaks.js").read_text(encoding="utf-8")
VERBS = (UI / "foundation" / "shell-verbs.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "styles.css").read_text(encoding="utf-8")

# The browser, as far as the theme code can see it: localStorage, an OS colour scheme that can change, and the document root's attributes.
# ``matches`` answers "does this query ask for the scheme the OS has now": the dark query is true while OS is "dark", the light query while it is "light".
_BROWSER = r"""
var STORE = {};
var localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(STORE, k) ? STORE[k] : null; },
  setItem: function (k, v) { STORE[k] = String(v); },
  removeItem: function (k) { delete STORE[k]; },
};
var OS = "dark", MQ = [];
window.matchMedia = function (q) {
  return { matches: q.indexOf(OS) >= 0, addEventListener: function (_t, cb) { MQ.push({ q: q, cb: cb }); } };
};
function osChanges(to) { OS = to; MQ.forEach(function (m) { m.cb({ matches: m.q.indexOf(OS) >= 0 }); }); }
var ATTR = {};
var document = { documentElement: {
  getAttribute: function (k) { return ATTR[k] || null; },
  setAttribute: function (k, v) { ATTR[k] = String(v); },
} };
"""

# What the menu reads from the shell: the signed-in user, the verb registry (a verb that records that it ran), the open-menu toggle and the shell's re-render.
_CONSOLE = r"""
window.parent = { postMessage: function () {} };
var LOG = { verbs: [], menus: [], bumps: 0, views: [] };
var __con = {
  username: "ana", role: "admin", view: { name: "studio", nav: null },
  bump: function () { LOG.bumps += 1; },
  toggleMenu: function (n) { LOG.menus.push(n); },
  registry: { get: function (id) { return { run: function () { LOG.verbs.push(id); } }; } },
};
function NV_useConsole() { return __con; }
window.confirmDialog = function () { return Promise.resolve(false); };
"""

# Every element at or under one, through function components and fragments (``MR.subtree`` reports tag names and a few attributes, not ``aria-hidden`` or children).
_COLLECT = r"""
function collect(node, out) {
  if (node == null || typeof node !== "object") return out;
  if (Array.isArray(node)) { node.forEach(function (c) { collect(c, out); }); return out; }
  if (!node.__el) return out;
  out.push(node);
  collect(typeof node.type === "function" && node.type !== React.Fragment ? node.out : node.children, out);
  return out;
}
"""

THEME_VERBS = {
    "view.profile": "Open Profile",
    "theme.light": "Switch To Light Theme",
    "theme.dark": "Switch To Dark Theme",
    "theme.system": "Switch To System Theme",
}


@pytest.fixture(scope="module")
def code() -> str:
    return TWEAKS + "\n" + transpile(CHROME_PATH)


@pytest.fixture
def app(code):
    """A fresh V8 context holding the real tweaks store and the real chrome; closed after the test."""
    ctx = mini_react_context(code, _BROWSER + _CONSOLE)

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

        def order(self, prefix: str) -> list[str]:
            """The test ids that start with ``prefix``, in the order they are drawn."""
            return json.loads(ctx.eval("JSON.stringify(MR.findAll(" + json.dumps(prefix) + ").map(function (e) { return e.props['data-testid']; }))"))

    try:
        yield App()
    finally:
        ctx.close()


def _page_after_reload(stored: dict, os_theme: str) -> tuple[object, str]:
    """A fresh page whose localStorage holds ``stored`` under an OS that prefers ``os_theme``: the first inline ``<script>`` of ``ui/index.html`` runs, then
    ``tweaks.js`` loads, exactly in the order the browser does it. Returns ``(context, data-theme)``; the caller closes the context."""
    from py_mini_racer import MiniRacer

    bootstrap = HTML[HTML.index("<script>") + len("<script>"):HTML.index("</script>")]
    ctx = MiniRacer()
    ctx.eval("var window = globalThis;" + _BROWSER)
    ctx.eval("STORE = " + json.dumps(stored) + "; OS = " + json.dumps(os_theme) + ";")
    ctx.eval(bootstrap)
    ctx.eval(TWEAKS)
    return ctx, ctx.eval('ATTR["data-theme"]')


# --- the helpers --------------------------------------------------------------------------------------------------------------------


def test_choosing_system_stores_system_per_user_and_null_in_the_tweak(app):
    app.js('NV_applyThemeChoice(__con, "system")')
    assert app.data('STORE["primer.theme.ana"]') == "system"
    assert json.loads(app.data('STORE["primer.tweaks"]')) == {"theme": None}
    assert app.data("LOG.bumps") == 1


@pytest.mark.parametrize("os_theme", ["light", "dark"])
def test_choosing_system_paints_the_os_theme_never_the_word_system(app, os_theme):   # Review Focus 1
    app.js(f'OS = "{os_theme}"; NV_applyThemeChoice(__con, "system")')
    assert app.data('ATTR["data-theme"]') == os_theme


def test_an_os_change_repaints_only_while_system_is_chosen(app):   # Review Focus 1
    app.js('NV_applyThemeChoice(__con, "system"); osChanges("light")')
    assert app.data('ATTR["data-theme"]') == "light"
    app.js('osChanges("dark")')
    assert app.data('ATTR["data-theme"]') == "dark", "System follows the OS in both directions"
    app.js('NV_applyThemeChoice(__con, "dark"); osChanges("dark"); osChanges("light")')
    assert app.data('ATTR["data-theme"]') == "dark", "an explicit Dark does not follow the OS"
    app.js('NV_applyThemeChoice(__con, "light"); osChanges("dark")')
    assert app.data('ATTR["data-theme"]') == "light", "an explicit Light does not follow the OS"
    app.js('NV_applyThemeChoice(__con, "system")')
    app.js('osChanges("light")')
    assert app.data('ATTR["data-theme"]') == "light", "and choosing System again follows it again"


@pytest.mark.parametrize("os_theme", ["light", "dark"])
def test_a_reload_with_system_chosen_resolves_from_the_os(app, os_theme):   # Review Focus 1
    """What localStorage holds after System was chosen is loaded by a fresh page: the bootstrap script and then ``tweaks.js``. The page paints what the OS
    prefers (never the word ``system``, which ``tweaks.js`` would apply as is if System were stored as the tweak's value), and it goes on following the OS."""
    app.js("OS = " + json.dumps("dark" if os_theme == "light" else "light") + '; NV_applyThemeChoice(__con, "system")')
    stored = app.data("STORE")
    ctx, painted = _page_after_reload(stored, os_theme)
    try:
        assert painted == os_theme
        other = "dark" if os_theme == "light" else "light"
        ctx.eval(f'osChanges("{other}")')
        assert ctx.eval('ATTR["data-theme"]') == other, "the reloaded page still follows the OS live"
    finally:
        ctx.close()


@pytest.mark.parametrize("choice", ["light", "dark"])
def test_a_reload_with_an_explicit_choice_keeps_it_whatever_the_os_prefers(app, choice):
    app.js(f'NV_applyThemeChoice(__con, "{choice}")')
    other = "dark" if choice == "light" else "light"
    ctx, painted = _page_after_reload(app.data("STORE"), other)
    try:
        assert painted == choice
        ctx.eval(f'osChanges("{other}")')
        assert ctx.eval('ATTR["data-theme"]') == choice
    finally:
        ctx.close()


def test_the_stored_choice_reads_back(app):
    assert app.data('NV_themeChoice("ana")') == "system"                                  # nothing stored
    app.js('STORE["primer.tweaks"] = JSON.stringify({ theme: "light" })')
    assert app.data('NV_themeChoice("ana")') == "light"                                   # an older browser-wide choice
    app.js('STORE["primer.theme.ana"] = "dark"')
    assert app.data('NV_themeChoice("ana")') == "dark"                                    # the user's own key wins
    app.js('STORE["primer.theme.ana"] = "system"')
    assert app.data('NV_themeChoice("ana")') == "system"
    app.js('STORE["primer.theme.ana"] = "purple"')
    assert app.data('NV_themeChoice("ana")') == "light"                                   # garbage falls back


def test_one_user_does_not_read_another_users_choice(app):
    app.js('STORE["primer.theme.bo"] = "dark"')
    assert app.data('NV_themeChoice("ana")') == "system"
    assert app.data('NV_themeChoice("bo")') == "dark"


def test_reading_the_choice_never_throws(app):
    app.js('localStorage.getItem = function () { throw new Error("blocked"); }')
    assert app.data('NV_themeChoice("ana")') == "system"
    app.js('localStorage.getItem = function () { return "{not json"; }')
    assert app.data('NV_themeChoice("ana")') == "system"


def test_light_and_dark_persist_exactly_as_before(app):
    app.js('NV_applyThemeChoice(__con, "dark")')
    assert app.data('STORE["primer.theme.ana"]') == "dark"
    assert json.loads(app.data('STORE["primer.tweaks"]')) == {"theme": "dark"}
    assert app.data('ATTR["data-theme"]') == "dark"


def test_a_value_that_is_not_a_choice_is_system_never_painted_as_is(app):   # Review Focus 1
    app.js('OS = "light"; NV_applyThemeChoice(__con, "purple")')
    assert app.data('ATTR["data-theme"]') == "light"
    assert app.data('STORE["primer.theme.ana"]') == "system"
    assert json.loads(app.data('STORE["primer.tweaks"]')) == {"theme": None}


def test_a_blocked_localstorage_does_not_stop_the_choice_taking_effect(app):
    app.js('localStorage.setItem = function () { throw new Error("quota"); }')
    app.js('NV_applyThemeChoice(__con, "light")')
    assert app.data('ATTR["data-theme"]') == "light"
    assert app.data("LOG.bumps") == 1, "the menu still re-renders to show the pressed choice"


def test_the_os_theme_is_dark_without_matchmedia(app):
    app.js("window.matchMedia = undefined")
    assert app.data("NV_osTheme()") == "dark"   # as the bootstrap script of index.html does


# --- the restore --------------------------------------------------------------------------------------------------------------------


def test_the_stored_choice_is_restored_once_the_user_is_known(app):
    app.js('STORE["primer.theme.ana"] = "system"; STORE["primer.tweaks"] = JSON.stringify({ theme: "dark" });'
           ' ATTR["data-theme"] = "dark"; OS = "light"')
    app.mount("NV_ActivityBar")
    assert app.data('ATTR["data-theme"]') == "light"
    assert json.loads(app.data('STORE["primer.tweaks"]')) == {"theme": None}


def test_an_explicit_stored_choice_is_restored_over_the_browsers_last_one(app):
    app.js('STORE["primer.theme.ana"] = "light"; STORE["primer.tweaks"] = JSON.stringify({ theme: "dark" }); ATTR["data-theme"] = "dark"')
    app.mount("NV_ActivityBar")
    assert app.data('ATTR["data-theme"]') == "light"
    assert json.loads(app.data('STORE["primer.tweaks"]')) == {"theme": "light"}


def test_a_user_who_never_chose_keeps_what_the_page_painted(app):
    app.js('ATTR["data-theme"] = "light"')
    app.mount("NV_ActivityBar")
    assert app.data('ATTR["data-theme"]') == "light"
    assert app.js('"primer.tweaks" in STORE') is False, "nothing is written for a user who has not chosen"
    assert app.data("LOG.bumps") == 0


def test_the_restore_runs_again_for_the_next_signed_in_user(app):
    app.js('STORE["primer.theme.bo"] = "light"; ATTR["data-theme"] = "dark"; __con.username = "ana"')
    app.mount("NV_ActivityBar")
    assert app.data('ATTR["data-theme"]') == "dark"
    app.js('__con.username = "bo"; MR.rerender()')
    assert app.data('ATTR["data-theme"]') == "light"


# --- the menu -----------------------------------------------------------------------------------------------------------------------


def test_the_menu_has_a_labelled_theme_row_with_three_choices(app):
    app.mount("NV_ProfileMenu")
    assert "Theme" in app.texts()
    assert app.attr("nv-theme-seg", "aria-labelledby") == "nv-theme-label"
    for value, name in (("light", "Light"), ("dark", "Dark"), ("system", "System")):
        assert app.attr(f"nv-theme:{value}", "data-verb") == f"theme.{value}"
        assert name in app.texts()
    assert [app.attr(f"nv-theme:{v}", "aria-pressed") for v in ("light", "dark", "system")] == ["false", "false", "true"]


def test_the_glyphs_are_decoration_so_a_choices_name_is_its_label(app):
    app.mount("NV_ProfileMenu")
    app.js(_COLLECT)
    for value, glyph in (("light", "\u2600"), ("dark", "\u263e"), ("system", "\u2318")):
        hidden = app.data(
            f'collect(MR.find("nv-theme:{value}"), []).filter(function (e) {{ return e.props && e.props["aria-hidden"] === "true"; }})'
            ".map(function (e) { return e.children; })"
        )
        assert hidden == [[glyph]], f"{value}: the glyph is the one aria-hidden thing in the button"


def test_the_pressed_choice_follows_what_is_stored(app):
    app.js('STORE["primer.theme.ana"] = "dark"')
    app.mount("NV_ProfileMenu")
    assert [app.attr(f"nv-theme:{v}", "aria-pressed") for v in ("light", "dark", "system")] == ["false", "true", "false"]
    assert [app.attr(f"nv-theme:{v}", "data-active") for v in ("light", "dark", "system")] == ["false", "true", "false"]


def test_clicking_a_choice_applies_it_and_presses_it(app):
    app.mount("NV_ProfileMenu")
    app.click("nv-theme:light")
    assert app.data('ATTR["data-theme"]') == "light" and app.data('STORE["primer.theme.ana"]') == "light"
    assert app.data("LOG.bumps") == 1, "the shell is asked to re-render, which is what redraws the pressed state in the real console"
    assert app.attr("nv-theme:light", "aria-pressed") == "true"
    app.click("nv-theme:system")
    assert app.data('STORE["primer.theme.ana"]') == "system"
    assert [app.attr(f"nv-theme:{v}", "aria-pressed") for v in ("light", "dark", "system")] == ["false", "false", "true"]


def test_the_menu_stays_open_while_a_theme_is_picked(app):
    app.mount("NV_ProfileMenu")
    app.click("nv-theme:dark")
    assert app.data("LOG.menus") == [], "picking a theme lets you compare it; only a navigating row closes the menu"


def test_your_profile_runs_the_open_profile_verb_and_closes_the_menu(app):
    app.mount("NV_ProfileMenu")
    app.click("nv-go-profile")
    assert app.data("LOG.verbs") == ["view.profile"] and app.data("LOG.menus") == [None]
    assert app.attr("nv-go-profile", "data-verb") == "view.profile"


@pytest.mark.parametrize("role", ["admin", "user", "restricted"])
def test_every_role_sees_your_profile(app, role):
    app.js(f'__con.role = "{role}"')
    app.mount("NV_ProfileMenu")
    assert app.has("nv-go-profile")
    assert app.has("nv-go-system") == (role != "restricted")
    assert app.has("nv-theme-row"), "the theme is a preference of the browser, not a privilege"


def test_your_profile_is_drawn_above_system_settings_and_the_sign_out_rows(app):
    app.mount("NV_ProfileMenu")
    assert app.order("nv-go-") == ["nv-go-profile", "nv-go-system"]
    drawn = app.order("nv-")
    assert drawn.index("nv-theme-row") < drawn.index("nv-go-profile") < drawn.index("nv-go-system") < drawn.index("nv-logout") < drawn.index("nv-logout-all")


# --- the verbs ----------------------------------------------------------------------------------------------------------------------


def _verb_block(src: str, verb_id: str) -> str:
    """The text of the ``reg({ ... })`` call that registers ``verb_id`` in the shell, braces balanced."""
    at = src.index(f'id: "{verb_id}"')
    start = src.rindex("reg({", 0, at)
    depth = 0
    for i in range(start + len("reg("), len(src)):
        depth += src[i] == "{"
        depth -= src[i] == "}"
        if depth == 0:
            return src[start:i + 2]   # through the closing "})"
    raise AssertionError(f"unbalanced reg() for {verb_id}")


@pytest.fixture
def verbs(app):
    """The four verbs exactly as ``nv-shell.jsx`` registers them, registered in the real verb registry (which lints the label and insists on a pointer
    surface) with the two things the shell closes over stood in: ``goView`` and ``themeEnvRef``."""
    app.js(VERBS)
    app.js(
        "var registry = SH_createVerbRegistry();"
        "function reg(v) { if (!registry.get(v.id)) registry.register(v); }"
        "var goView = function (name, nav) { LOG.views.push([name, nav || null]); };"
        "var themeEnvRef = { current: __con };"
    )
    app.js(";\n".join(_verb_block(SHELL, verb_id) for verb_id in THEME_VERBS) + ";")
    return app


@pytest.mark.parametrize(("verb_id", "label"), list(THEME_VERBS.items()))
def test_the_verbs_register_with_labels_the_registry_accepts(verbs, verb_id, label):
    assert verbs.data(f'registry.get("{verb_id}").canonical') == label
    assert verbs.data(f'registry.get("{verb_id}").surfaces') == ["topbar", "palette"]
    assert verbs.data(f"SH_lintVerbLabel({json.dumps(label)})") is None


def test_open_profile_opens_system_profile(verbs):
    verbs.js('registry.get("view.profile").run()')
    assert verbs.data("LOG.views") == [["system", "profile"]]


@pytest.mark.parametrize("choice", ["light", "dark", "system"])
def test_the_theme_verbs_apply_through_the_one_shared_helper(verbs, choice):
    verbs.js(f'OS = "light"; registry.get("theme.{choice}").run()')
    assert verbs.data('STORE["primer.theme.ana"]') == choice
    assert verbs.data('ATTR["data-theme"]') == ("light" if choice == "system" else choice)
    assert json.loads(verbs.data('STORE["primer.tweaks"]')) == {"theme": None if choice == "system" else choice}
    assert verbs.data("LOG.bumps") == 1


def test_the_shell_refreshes_the_environment_the_verbs_run_against_every_render():
    """The verbs are registered once, in an effect that never re-runs, so they read this ref when they RUN. A ref that kept the first render's username would store
    the choice of whoever was signed in at mount and the menu would not show it."""
    assert re.search(r"var themeEnvRef = React\.useRef\(", SHELL)
    refresh = re.search(r"themeEnvRef\.current = \{([\s\S]*?)\};", SHELL)
    assert refresh, "the ref is assigned on every render, not only at creation"
    assert "username: NV_usernameOf(status.data)" in refresh.group(1)
    assert "setTick(" in refresh.group(1), "bump re-renders the shell, which redraws the menu"
    assert SHELL.index("themeEnvRef.current =") < SHELL.index('id: "theme.light"')


# --- the style ----------------------------------------------------------------------------------------------------------------------


def test_the_theme_row_restores_the_full_width_the_later_nv_seg_rule_takes_away():
    """``.nv-seg`` is declared twice and the later one (the new-session overlay's segmented control) is ``display: inline-flex``, which shrink-wrapped the
    menu's control. The row says it needs the width with a selector that out-ranks both."""
    rule = re.search(r"\.nv-theme-row \.nv-seg\s*\{([^}]*)\}", CSS)
    assert rule, "the Theme row restores the width with a descendant selector"
    assert re.search(r"display:\s*flex\b", rule.group(1)) and re.search(r"width:\s*100%", rule.group(1))
    assert re.search(r"\.nv-theme-label\s*\{", CSS)


def test_the_gap_after_a_glyph_is_its_own_margin_not_a_space_character():
    """The phone's buttons are ``.touch-target``, i.e. ``display: inline-flex``, and a flex container drops the whitespace at the start of a text run: a space
    between the glyph and the label showed as "Light" with the glyph touching it on the phone (a real browser, not V8, shows it)."""
    rule = re.search(r"\.nv-theme-glyph\s*\{([^}]*)\}", CSS)
    assert rule and re.search(r"margin-right:\s*\d+px", rule.group(1))
    chrome = CHROME_PATH.read_text(encoding="utf-8")
    label = chrome[chrome.index("function NV_ThemeChoiceLabel"):]
    label = label[:label.index("\n}\n")]
    assert 'className="nv-theme-glyph"' in label and '{" "}' not in label
