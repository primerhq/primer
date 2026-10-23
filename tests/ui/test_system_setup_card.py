"""Tests for the Dashboard Setup card (Revamp 10, Task 7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
SETUP_WIZARD = ROOT / "ui" / "components" / "setup-wizard.jsx"
CHROME = ROOT / "ui" / "components" / "console" / "nv-chrome.jsx"
KIT = ROOT / "ui" / "components" / "shared" / "platform-kit.jsx"
SYSTEM = ROOT / "ui" / "components" / "console" / "nv-system.jsx"
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")
USE_RESOURCE = (ROOT / "ui" / "foundation" / "use-resource.js").read_text(encoding="utf-8")

_PREDICATES = [
    ("llm_provider", "LLM provider responds"),
    ("model_profile", "Model profile registered"),
    ("default_workspace", "Default workspace reachable"),
    ("operator_agent", "Operator agent seeded"),
    ("builder_agent", "Builder agent seeded"),
    ("system_collection", "System collection exists"),
]


def _state(failing: dict | None = None) -> dict:
    failing = failing or {}
    predicates = []
    for key, label in _PREDICATES:
        ok = key not in failing
        detail = failing.get(key)
        predicates.append({"key": key, "label": label, "ok": ok, "detail": detail})
    return {"predicates": predicates}


PRELUDE = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8") + r"""
var window = globalThis;
var DATA = {};
var ERRORS = {};
var REPLIES = {};
var LOG = { calls: [], opts: {}, views: [], toasts: [], confirms: [] };
var CONFIRM = true;

window.primerApi = window.primerApi || {};

window.primerApi.useResource = function (key, fetcher, opts) {
  LOG.opts[key] = opts || {};
  return {
    data: DATA[key] || null,
    error: ERRORS[key] || null,
    loading: !(key in DATA) && !(key in ERRORS),
    refetch: function () {
      LOG.calls.push(["REFETCH", key]);
      return Promise.resolve();
    }
  };
};

window.primerApi.resourceState = function (res) {
  if (res && res.error) return "stuck";
  if (res && res.data) return "ready";
  return "loading";
};

window.primerApi.apiFetch = function (method, path, body, opts) {
  LOG.calls.push([method, path]);
  var key = method + " " + path;
  var rep = REPLIES[key];
  if (rep && rep.pending) return new Promise(function () {});
  if (rep && rep.err) return Promise.reject(rep.err);
  if (rep && rep.ok !== undefined) return Promise.resolve(rep.ok);
  return Promise.resolve({});
};

window.primerApi.useCapabilities = function () {
  return DATA.capabilities || null;
};

window.primerApi.useEscape = function (fn, active) {};

window.confirmDialog = function (opts) {
  LOG.confirms.push(opts);
  return Promise.resolve(CONFIRM);
};

var __con = {
  openMenu: null,
  toggleMenu: function (name) {
    __con.openMenu = __con.openMenu === name ? null : name;
    if (typeof MR !== "undefined" && MR.rerender) MR.rerender();
  },
  goView: function (sec, nav) {
    LOG.views.push([sec, nav]);
  },
  toast: function (text, kind) {
    LOG.toasts.push([text, kind]);
  }
};
window.NV_useConsole = function () { return __con; };

function Icon(p) {
  return React.createElement("span", { className: "ico", "data-icon": p.name });
}
function Btn(p) {
  var props = Object.assign({}, p);
  props["data-testid"] = p["data-testid"] || p.testid;
  return React.createElement("button", props, p.children);
}
var SH_api = {};
function relativeTime(s) { return String(s); }
function NV_identity(b) { return { d: "", color: "" }; }
"""


# The card's other dependencies, stubbed for the tests that run the REAL use-resource.js (whose resourceState is not replaced).
_REAL_RESOURCE_STUBS = r"""
window.primerApi.useCapabilities = function () { return { data: null, loading: true, error: null }; };
window.primerApi.useEscape = function () {};
window.confirmDialog = function () { return Promise.resolve(false); };
var __con = {
  openMenu: null,
  toggleMenu: function (name) { __con.openMenu = __con.openMenu === name ? null : name; MR.rerender(); },
  goView: function () {}, toast: function () {}
};
window.NV_useConsole = function () { return __con; };
function Icon(p) { return React.createElement("span", { "data-icon": p.name }); }
function Btn(p) { return React.createElement("button", Object.assign({}, p), p.children); }
var SH_api = {};
function relativeTime(s) { return String(s); }
function NV_identity() { return { d: "", color: "" }; }
"""


@pytest.fixture(scope="module")
def code() -> str:
    return (
        transpile(SETUP_WIZARD)
        + "\n"
        + transpile(CHROME)
        + "\n"
        + transpile(KIT)
        + "\n"
        + transpile(SYSTEM)
    )


@pytest.fixture
def app(code):
    ctx = mini_react_context(code, PRELUDE)

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
            return json.loads(
                ctx.eval(
                    f"JSON.stringify({{v: (MR.find({json.dumps(testid)}) ? (MR.find({json.dumps(testid)}).props[{json.dumps(name)}]) : null)}})"
                )
            ).get("v")

        def texts(self) -> list[str]:
            return json.loads(ctx.eval("JSON.stringify(MR.texts())"))

        def text(self) -> str:
            return "".join(self.texts())

        def text_of(self, testid: str) -> str:
            code = f"""
            (function() {{
                var el = MR.find({json.dumps(testid)});
                if (!el) return "";
                var out = [];
                (function walk(n) {{
                    if (n == null || typeof n === "boolean") return;
                    if (typeof n === "string" || typeof n === "number") {{ out.push(String(n)); return; }}
                    if (Array.isArray(n)) {{ n.forEach(walk); return; }}
                    if (n.__el) {{
                        if (typeof n.type === "function" && n.type !== React.Fragment) {{
                            walk(n.out);
                        }} else {{
                            walk(n.children || (n.props && n.props.children));
                        }}
                    }}
                }})(el);
                return out.join("");
            }})()
            """
            return ctx.eval(code)

        def find_all(self, prefix: str) -> list[dict]:
            return json.loads(ctx.eval("JSON.stringify(MR.findAll(" + json.dumps(prefix) + "))"))

        def settle(self) -> None:
            ctx.eval("void 0;")
            ctx.eval("MR.rerender();")
            ctx.eval("void 0;")
            ctx.eval("MR.rerender();")

    try:
        yield App()
    finally:
        ctx.close()


def test_the_card_view_of_a_complete_install(app):
    assert app.data(f"NV_setupCardView({json.dumps(_state())})") == {
        "tone": "ok",
        "headline": "All 6 setup checks pass",
        "failing": [],
    }


def test_the_card_view_lists_each_failing_check_with_its_fix(app):
    v = app.data(
        f"NV_setupCardView({json.dumps(_state({'llm_provider': 'no LLM provider configured', 'operator_agent': 'row does not exist'}))})"
    )
    assert v["tone"] == "error" and v["headline"] == "2 of 6 setup checks fail"
    assert [(f["key"], f["fix"]) for f in v["failing"]] == [
        ("llm_provider", {"label": "Open Providers", "action": "providers"}),
        ("operator_agent", {"label": "Re-run seed", "action": "seed"}),
    ]
    assert (
        app.data(f"NV_setupCardView({json.dumps(_state({'system_collection': None}))})")[
            "headline"
        ]
        == "1 of 6 setup checks fails"
    )


def test_the_seed_outcome_says_what_happened(app):
    assert app.data('NV_seedOutcome({ created: ["operator", "builder"], errors: [] })') == {
        "kind": "info",
        "text": "Seed ran: created operator, builder.",
    }
    assert app.data(
        'NV_seedOutcome({ created: [], skipped: ["operator"], errors: [] })'
    ) == {"kind": "info", "text": "Seed ran: nothing was missing."}


def test_long_seed_error_message_is_truncated(app):
    long_msg = "x" * 500
    res = app.data(f'NV_seedOutcome({{ errors: [["test_check", {json.dumps(long_msg)}]] }})')
    assert res["kind"] == "error"
    expected = "Seed finished with errors: test_check: " + ("x" * 200) + "…"
    assert res["text"] == expected


def test_seed_error_messages_are_cleaned_before_the_length_limit(app):
    result = app.data(
        "NV_seedOutcome({ errors: [[\"default_workspace\", "
        "\"ConnectError('https://host/v1?key=AIzaSECRET Authorization: Bearer sk-live-abc')\"]] })"
    )
    assert result["kind"] == "error"
    assert "AIzaSECRET" not in result["text"]
    assert "sk-live-abc" not in result["text"]
    assert result["text"].count("[REDACTED]") == 2


def test_the_capability_rows(app):
    rows = app.data(
        "NV_capabilityRows({ extras: { lance: { installed: false }, docker: { installed: true } }, speech: { stt_configured: true, tts_configured: false } })"
    )
    assert rows == [
        {"name": "docker", "ok": True, "text": "container workspace backend"},
        {"name": "lance", "ok": False, "text": "local semantic-search vector store"},
        {"name": "speech", "ok": True, "text": "stt configured, tts not configured"},
    ]


def test_the_card_reads_setup_state_once_and_never_polls(app):  # Review Focus 4
    app.mount("NV_SysSetupCard")
    assert not app.data('LOG.opts["nv-sys:setup-state"]').get("pollMs")
    assert app.data('LOG.opts["nv-sys:setup-state"]').get("refetchOnWake") is False


def test_a_tab_reshow_or_an_idle_wake_does_not_read_setup_state_again(code):  # Review Focus 4, audit G1-09
    """The real use-resource.js: each read lists the LLM's models and re-attaches the default workspace, so only Check again, a seed or a reset may
    read it again, not the page becoming visible or the operator coming back from idle."""
    prelude = (
        "var window = globalThis;\n"
        + API
        + "\nvar VIS = null;\n"
        + "var document = { hidden: false, addEventListener: function (n, fn) { if (n === 'visibilitychange') VIS = fn; } };\n"
        + "function AbortController() { this.signal = { aborted: false }; this.abort = function () { this.signal.aborted = true; }; }\n"
        + USE_RESOURCE
        + _REAL_RESOURCE_STUBS
        + r"""
var CALLS = 0;
window.primerApi.apiFetch = function (method, path) {
  if (method === "GET" && path === "/setup/state") {
    CALLS += 1;
    return Promise.resolve({ predicates: [] });
  }
  return Promise.resolve({});
};
"""
    )
    ctx = mini_react_context(code, prelude)
    try:
        ctx.eval("MR.mount(NV_SysSetupCard, {})")
        for _ in range(3):
            ctx.eval("void 0; MR.rerender();")
        assert ctx.eval("CALLS") == 1
        ctx.eval("document.hidden = true; VIS(); document.hidden = false; VIS();")
        ctx.eval("window.primerApi._refetchAll();")
        for _ in range(3):
            ctx.eval("void 0; MR.rerender();")
        assert ctx.eval("CALLS") == 1, "a re-show or a wake read /setup/state again"
        ctx.eval('MR.click("nv-sys-setup-menu-btn"); MR.click("nv-sys-setup-recheck");')
        for _ in range(3):
            ctx.eval("void 0; MR.rerender();")
        assert ctx.eval("CALLS") == 2, "Check again still reads it"
    finally:
        ctx.close()


def test_a_green_card_says_all_checks_pass(app):
    app.js(f'DATA["nv-sys:setup-state"] = {json.dumps(_state())}')
    app.mount("NV_SysSetupCard")
    assert (
        app.attr("nv-sys-setup", "data-tone") == "ok"
        and app.text_of("nv-sys-setup-headline") == "All 6 setup checks pass"
    )
    assert app.find_all("nv-sys-setup-check:") == []
    assert app.text_of("nv-sys-setup-title") == "Setup"


def test_open_providers_goes_to_the_providers_page(app):
    app.js(
        f'DATA["nv-sys:setup-state"] = {json.dumps(_state({"llm_provider": "no LLM provider configured"}))}'
    )
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-fix:llm_provider")
    assert app.data("LOG.views") == [["platform", "providers"]]


def test_a_detail_with_credentials_is_shown_without_them(app):
    app.js(
        f'DATA["nv-sys:setup-state"] = {json.dumps(_state({"llm_provider": "connection refused: http://user:s3cret@host.example/v1/models"}))}'
    )
    app.mount("NV_SysSetupCard")
    assert "host.example/v1/models" in app.text() and "s3cret" not in app.text()


def test_re_run_seed_posts_then_checks_again_and_says_what_it_did(app):
    app.js(
        f'DATA["nv-sys:setup-state"] = {json.dumps(_state())};'
        ' REPLIES["POST /setup/seed"] = { ok: { created: ["operator"], skipped: [], errors: [] } }'
    )
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-menu-btn")
    app.click("nv-sys-setup-rerun-seed")
    app.settle()
    assert app.data("LOG.calls") == [["POST", "/setup/seed"], ["REFETCH", "nv-sys:setup-state"]]
    assert app.data("LOG.toasts") == [["Seed ran: created operator.", "info"]]


def test_a_seed_that_answers_with_errors_is_reported_as_failed(app):  # Review Focus 3
    app.js(
        f'DATA["nv-sys:setup-state"] = {json.dumps(_state())};'
        ' REPLIES["POST /setup/seed"] = { ok: { created: [], skipped: [], errors: [["system_collection", "RuntimeError(\'boom\')"]] } }'
    )
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-menu-btn")
    app.click("nv-sys-setup-rerun-seed")
    app.settle()
    [(text, kind)] = app.data("LOG.toasts")
    assert (
        kind == "error"
        and text == "Seed finished with errors: system_collection: RuntimeError('boom')"
    )


def test_seed_errors_use_the_exact_separator(app):
    result = app.data('NV_seedOutcome({ errors: [["a", "x"], ["b", "y"]] })')
    assert result["text"] == "Seed finished with errors: a: x; b: y"


def test_seed_busy_copy_is_exact(app):
    app.js(
        f'DATA["nv-sys:setup-state"] = {json.dumps(_state())};'
        ' REPLIES["POST /setup/seed"] = { pending: true }'
    )
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-menu-btn")
    app.click("nv-sys-setup-rerun-seed")
    assert app.text_of("nv-sys-setup-headline") == "Running the seed…"


def test_seed_failure_toast_is_pinned(app):
    app.js(
        f'DATA["nv-sys:setup-state"] = {json.dumps(_state())};'
        ' REPLIES["POST /setup/seed"] = { err: { detail: "upstream down", requestId: "req-seed" } }'
    )
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-menu-btn")
    app.click("nv-sys-setup-rerun-seed")
    app.settle()
    assert app.data("LOG.toasts") == [[
        "Re-run seed failed: upstream down",
        {"kind": "error", "requestId": "req-seed"},
    ]]


def test_reset_default_agents_asks_first_and_a_declined_confirm_posts_nothing(
    app,
):  # Review Focus 2
    app.js(f'DATA["nv-sys:setup-state"] = {json.dumps(_state())}; CONFIRM = false')
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-menu-btn")
    app.click("nv-sys-setup-reset-agents")
    app.settle()
    [confirm] = app.data("LOG.confirms")
    assert confirm == {
        "title": "Reset default agents",
        "message": "This restores the description, system prompt and tools of operator, builder, planner, explorer and tool-runner to their defaults. Your edits to those agents are lost; the model profile each one runs on is kept.",
        "confirmLabel": "Reset agents",
        "danger": True,
    }
    assert ["POST", "/setup/reset_agents"] not in app.data("LOG.calls")


def test_a_confirmed_reset_posts_and_names_the_agents(app):
    app.js(
        f'DATA["nv-sys:setup-state"] = {json.dumps(_state())}; CONFIRM = true;'
        ' REPLIES["POST /setup/reset_agents"] = { ok: { reset: ["operator", "builder", "planner", "explorer", "tool-runner"] } }'
    )
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-menu-btn")
    app.click("nv-sys-setup-reset-agents")
    app.settle()
    assert ["POST", "/setup/reset_agents"] in app.data("LOG.calls")
    assert app.data("LOG.toasts") == [
        [
            "Reset the default agents: operator, builder, planner, explorer, tool-runner.",
            "info",
        ]
    ]


def test_reset_busy_copy_is_exact(app):
    app.js(
        f'DATA["nv-sys:setup-state"] = {json.dumps(_state())}; CONFIRM = true;'
        ' REPLIES["POST /setup/reset_agents"] = { pending: true }'
    )
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-menu-btn")
    app.click("nv-sys-setup-reset-agents")
    app.settle()
    assert app.text_of("nv-sys-setup-headline") == "Resetting the default agents…"


def test_reset_failure_toast_is_pinned(app):
    app.js(
        f'DATA["nv-sys:setup-state"] = {json.dumps(_state())}; CONFIRM = true;'
        ' REPLIES["POST /setup/reset_agents"] = { err: { detail: "write refused", requestId: "req-reset" } }'
    )
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-menu-btn")
    app.click("nv-sys-setup-reset-agents")
    app.settle()
    assert app.data("LOG.toasts") == [[
        "Reset failed: write refused",
        {"kind": "error", "requestId": "req-reset"},
    ]]


def test_a_failed_read_says_so_and_still_offers_check_again(app):
    app.js('ERRORS["nv-sys:setup-state"] = { detail: "upstream down", status: 503 }')
    app.mount("NV_SysSetupCard")
    assert app.text_of("nv-sys-setup-headline") == "Couldn't check setup"
    assert app.text_of("nv-sys-setup-retry") == "Retry"
    app.click("nv-sys-setup-menu-btn")
    assert app.has("nv-sys-setup-recheck")


def test_a_failed_read_uses_the_failed_state_from_the_real_resource(code):
    """A no-poll resource fails once, so it is errored but never reaches resourceState's three-error stuck threshold."""
    prelude = (
        "var window = globalThis;\n"
        + API
        + "\nvar document = { hidden: false, addEventListener: function () {} };\n"
        + "function AbortController() { this.signal = { aborted: false }; this.abort = function () { this.signal.aborted = true; }; }\n"
        + USE_RESOURCE
        + r"""
var CALLS = 0;
window.primerApi.apiFetch = function (method, path) {
  if (method === "GET" && path === "/setup/state") {
    CALLS += 1;
    return Promise.reject({ status: 503, detail: "upstream down" });
  }
  return Promise.resolve({});
};
"""
        + _REAL_RESOURCE_STUBS
    )
    ctx = mini_react_context(code, prelude)
    try:
        ctx.eval("MR.mount(NV_SysSetupCard, {})")
        for _ in range(4):
            ctx.eval("void 0; MR.rerender();")
        assert ctx.eval('MR.find("nv-sys-setup-headline").children.join("")') == "Couldn't check setup"
        assert ctx.eval('MR.find("nv-sys-setup-retry").children.join("")') == "Retry"
        assert ctx.eval("CALLS") == 1
        ctx.eval('MR.click("nv-sys-setup-retry")')
        for _ in range(4):
            ctx.eval("void 0; MR.rerender();")
        assert ctx.eval("CALLS") == 2
    finally:
        ctx.close()


def test_the_card_has_the_installed_extras_fold(app):
    app.js(f'DATA["nv-sys:setup-state"] = {json.dumps(_state())}')
    app.mount("NV_SysSetupCard")
    assert app.has("nv-sys-setup-extras")


# ---- the Setup menu's keyboard (audit G1-14) and a long failing detail (audit G1-15) ----------------------------------------------------------------

# A stand-in for React attaching the menu's ref to its DOM node before the effects run (the harness has no DOM): the list node, whose items take
# the focus as a browser's would (the document's active element moves).
_MENU_DOM = r"""
var document = (typeof document !== "undefined" && document) || {};
var FOCUSED = null;
var MENU_ITEMS = ["nv-sys-setup-recheck", "nv-sys-setup-rerun-seed", "nv-sys-setup-reset-agents"].map(function (id) {
  return { id: id, focus: function () { FOCUSED = id; document.activeElement = this; } };
});
var MENU_NODE = {
  querySelector: function () { return MENU_ITEMS[0]; },
  querySelectorAll: function () { return MENU_ITEMS; },
};
var PREVENTED = 0;
(function () {
  var inner = React.createElement;
  React.createElement = function (type, props) {
    if (props && props["data-testid"] === "nv-sys-setup-menu" && props.ref && typeof props.ref === "object") props.ref.current = MENU_NODE;
    return inner.apply(null, arguments);
  };
})();
function menuKey(k) {
  MR.find("nv-sys-setup-menu").props.onKeyDown({
    key: k, currentTarget: MENU_NODE, preventDefault: function () { PREVENTED++; },
  });
}
"""


def test_opening_the_setup_menu_moves_the_focus_to_its_first_item(app):
    app.js(f'DATA["nv-sys:setup-state"] = {json.dumps(_state())};' + _MENU_DOM)
    app.mount("NV_SysSetupCard")
    assert app.js("FOCUSED") is None
    app.click("nv-sys-setup-menu-btn")
    assert app.js("FOCUSED") == "nv-sys-setup-recheck"


def test_the_arrow_keys_home_and_end_move_between_the_setup_menus_items(app):
    app.js(f'DATA["nv-sys:setup-state"] = {json.dumps(_state())};' + _MENU_DOM)
    app.mount("NV_SysSetupCard")
    app.click("nv-sys-setup-menu-btn")
    app.js('menuKey("ArrowDown")')
    assert app.js("FOCUSED") == "nv-sys-setup-rerun-seed"
    app.js('menuKey("ArrowDown"); menuKey("ArrowDown")')
    assert app.js("FOCUSED") == "nv-sys-setup-recheck", "it wraps from the last item to the first"
    app.js('menuKey("ArrowUp")')
    assert app.js("FOCUSED") == "nv-sys-setup-reset-agents", "and from the first to the last"
    app.js('menuKey("Home")')
    assert app.js("FOCUSED") == "nv-sys-setup-recheck"
    app.js('menuKey("End")')
    assert app.js("FOCUSED") == "nv-sys-setup-reset-agents"
    assert app.js("PREVENTED") == 6
    app.js('menuKey("Tab"); menuKey("a")')
    assert app.js("PREVENTED") == 6, "any other key is left alone, Tab included"


def test_a_long_failing_detail_wraps_anywhere_instead_of_widening_the_row(app):
    long_url = "connection refused: https://registry.example/" + "a" * 120 + "/v1/models"
    app.js(f'DATA["nv-sys:setup-state"] = {json.dumps(_state({"llm_provider": long_url}))}')
    app.mount("NV_SysSetupCard")
    style = app.attr("nv-sys-setup-detail:llm_provider", "style") or {}
    assert style.get("overflowWrap") == "anywhere", style
