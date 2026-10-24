"""The "Reset prime instructions" action (pivot P2 Task 15) on the Dashboard's Setup card and in the palette.

``POST /v1/setup/reset_prime_instructions`` (admin only) overwrites ``agents.md`` in the default workspace (prime) with the version the platform
ships, and answers ``{workspace_id, path, bytes, shadows}``; ``shadows`` is ``"AGENTS.md"`` when an ``AGENTS.md`` is beside the file (the loader reads
``AGENTS.md`` only while ``agents.md`` is absent, so it is no longer read). The Setup card's menu (``NV_SysSetupCard``, ``nv-system.jsx``) offers the
action to an admin only: hidden, not disabled, for anyone else. It asks first and says what it overwrites, is busy while the request is out, says
what it did (and, with ``shadows``, that the loader now reads ``agents.md``) and says a refusal in ``readRefusal``'s words. The palette verb
``setup.reset_prime_instructions`` runs the same action and is offered to an admin only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._support.js_source import blank, close_of
from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SYSTEM = UI / "components" / "console" / "nv-system.jsx"
KIT = UI / "components" / "shared" / "platform-kit.jsx"
CHROME = UI / "components" / "console" / "nv-chrome.jsx"
SHELL = (UI / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
PALETTE = (UI / "components" / "console" / "nv-palette.jsx").read_text(encoding="utf-8")
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")
VERBS = (UI / "foundation" / "shell-verbs.js").read_text(encoding="utf-8")

OK_STATE = {"predicates": [{"key": "llm_provider", "label": "LLM provider responds", "ok": True, "detail": None}]}

PRELUDE = API + "\nvar DATA = { \"nv-sys:setup-state\": " + json.dumps(OK_STATE) + " };\n" + r"""
var REPLIES = {};
var LOG = { calls: [], toasts: [], confirms: [], pushed: [] };
var CONFIRM = true;
window.primerApi.useResource = function (key) {
  return { data: DATA[key] || null, error: null, loading: !(key in DATA), refetch: function () { LOG.calls.push(["REFETCH", key]); return Promise.resolve(); } };
};
window.primerApi.resourceState = function (res) { return res && res.data ? "ready" : "loading"; };
window.primerApi.apiFetch = function (method, path) {
  LOG.calls.push([method, path]);
  var rep = REPLIES[method + " " + path];
  if (rep && rep.pending) return new Promise(function () {});
  if (rep && rep.err) return Promise.reject(rep.err);
  return Promise.resolve(rep && rep.ok !== undefined ? rep.ok : {});
};
window.primerApi.toastPush = function (t) { LOG.pushed.push(t); };
window.primerApi.useCapabilities = function () { return null; };
window.primerApi.useEscape = function () {};
window.confirmDialog = function (opts) { LOG.confirms.push(opts); return Promise.resolve(CONFIRM); };
var __con = {
  role: "admin", openMenu: null,
  toggleMenu: function (name) { __con.openMenu = __con.openMenu === name ? null : name; if (typeof MR !== "undefined" && MR.rerender) MR.rerender(); },
  goView: function () {},
  toast: function (text, kind) { LOG.toasts.push([text, kind]); },
};
window.NV_useConsole = function () { return __con; };
function Icon(p) { return React.createElement("span", { "data-icon": p.name }); }
function Btn(p) { return React.createElement("button", Object.assign({}, p), p.children); }
var SH_api = {};
function relativeTime(s) { return String(s); }
function NV_identity() { return { d: "", color: "" }; }
"""

RESULT = {"workspace_id": "prime", "path": "agents.md", "bytes": 4120, "shadows": None}


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(CHROME) + "\n" + transpile(KIT) + "\n" + transpile(SYSTEM)


@pytest.fixture
def card(code):
    ctx = mini_react_context(code, PRELUDE)

    class Card:
        mounted = False

        def js(self, expr: str):
            return json.loads(ctx.eval(f"JSON.stringify({{v: {expr}}})")).get("v")

        def run(self, stmt: str) -> None:
            ctx.eval(stmt)
            for _ in range(3):  # let the confirm and the request settle, re-rendering after each step
                ctx.eval("void 0;" + (" MR.rerender();" if self.mounted else ""))

        def mount(self, role: str | None = "admin") -> None:
            self.mounted = True
            self.run(f"__con.role = {json.dumps(role)}; MR.mount(NV_SysSetupCard, {{}})")

        def open_menu(self) -> None:
            self.run('MR.click("nv-sys-setup-menu-btn")')

        def has(self, testid: str) -> bool:
            return bool(ctx.eval(f"!!MR.find({json.dumps(testid)})"))

        def prop(self, testid: str, name: str):
            return self.js(f"MR.find({json.dumps(testid)}).props[{json.dumps(name)}]")

        def text_of(self, testid: str) -> str:
            return "".join(self.js(
                f"(function () {{ var out = []; (function walk(n) {{ if (n == null || typeof n === 'boolean') return;"
                f" if (typeof n === 'string' || typeof n === 'number') {{ out.push(String(n)); return; }}"
                f" if (Array.isArray(n)) {{ n.forEach(walk); return; }} if (n.__el) walk(typeof n.type === 'function' ? n.out : n.children); }})"
                f"(MR.find({json.dumps(testid)})); return out; }})()"
            ))

    try:
        yield Card()
    finally:
        ctx.close()


def test_an_admin_is_offered_the_reset_with_its_verb(card) -> None:
    card.mount("admin")
    card.open_menu()
    assert card.has("nv-sys-setup-reset-prime")
    assert card.prop("nv-sys-setup-reset-prime", "data-verb") == "setup.reset_prime_instructions"
    assert card.prop("nv-sys-setup-reset-prime", "data-danger") == "true"
    assert card.text_of("nv-sys-setup-reset-prime") == "Reset prime instructions"


@pytest.mark.parametrize("role", ["user", "restricted", None])
def test_anyone_else_is_not_shown_it(card, role) -> None:
    """Hidden, not disabled: the route answers 403 to anyone but an admin."""
    card.mount(role)
    card.open_menu()
    assert card.has("nv-sys-setup-recheck"), "the menu itself is open"
    assert not card.has("nv-sys-setup-reset-prime")


def test_it_asks_first_and_says_what_it_overwrites_and_a_declined_confirm_sends_nothing(card) -> None:
    card.run("CONFIRM = false")
    card.mount()
    card.open_menu()
    card.run('MR.click("nv-sys-setup-reset-prime")')
    [confirm] = card.js("LOG.confirms")
    assert confirm == {
        "title": "Reset prime instructions",
        "message": "This overwrites agents.md in the default workspace (prime) with the instructions the platform ships. Your edits to that "
                   "file are lost. An AGENTS.md beside it is left in place, and is not read while agents.md exists.",
        "confirmLabel": "Reset instructions",
        "danger": True,
    }
    assert card.js("LOG.calls") == [] and card.js("LOG.toasts") == []


def test_a_confirmed_reset_posts_once_and_says_what_it_did(card) -> None:
    card.run(f'REPLIES["POST /setup/reset_prime_instructions"] = {{ ok: {json.dumps(RESULT)} }}')
    card.mount()
    card.open_menu()
    card.run('MR.click("nv-sys-setup-reset-prime")')
    assert card.js("LOG.calls") == [["POST", "/setup/reset_prime_instructions"]]
    assert card.js("LOG.toasts") == [["Prime instructions reset: agents.md in prime is the shipped version again.", "success"]]
    assert not card.has("nv-sys-setup-prime-shadows")


def test_an_agents_md_beside_it_is_named_and_the_loader_reads_agents_md_instead(card) -> None:
    shadowed = dict(RESULT, workspace_id="primer", shadows="AGENTS.md")
    card.run(f'REPLIES["POST /setup/reset_prime_instructions"] = {{ ok: {json.dumps(shadowed)} }}')
    card.mount()
    card.open_menu()
    card.run('MR.click("nv-sys-setup-reset-prime")')
    note = "AGENTS.md is beside agents.md in primer: the loader now reads agents.md instead, so AGENTS.md is no longer read."
    assert card.js("LOG.toasts") == [["Prime instructions reset: agents.md in primer is the shipped version again. " + note, "success"]]
    assert card.text_of("nv-sys-setup-prime-shadows") == note, "a toast goes away; the card keeps the line"


def test_the_card_is_busy_while_the_request_is_out(card) -> None:
    card.run('REPLIES["POST /setup/reset_prime_instructions"] = { pending: true }')
    card.mount()
    card.open_menu()
    card.run('MR.click("nv-sys-setup-reset-prime")')
    assert card.text_of("nv-sys-setup-headline") == "Resetting prime instructions…"
    card.run('MR.click("nv-sys-setup-menu-btn")')
    assert not card.has("nv-sys-setup-menu"), "no second action while one is out"


def test_a_refusal_is_said_in_the_readers_words_never_as_a_bare_code(card) -> None:
    card.run(
        'REPLIES["POST /setup/reset_prime_instructions"] = { err: new window.primerApi.ApiError({ status: 404, title: "Not Found",'
        ' detail: "no default workspace is recorded", extensions: { code: "not_found", request_id: "req-prime" } }) }'
    )
    card.mount()
    card.open_menu()
    card.run('MR.click("nv-sys-setup-reset-prime")')
    expected = card.js(
        'window.primerApi.readRefusal(new window.primerApi.ApiError({ status: 404, title: "Not Found", detail: "no default workspace is recorded",'
        ' extensions: { code: "not_found" } })).message'
    )
    assert "no default workspace is recorded" in expected and "not_found" not in expected
    [(text, extra)] = card.js("LOG.toasts")
    assert text == "Couldn't reset prime instructions: " + expected
    assert extra == {"kind": "error", "requestId": "req-prime"}
    assert card.text_of("nv-sys-setup-headline") != "Resetting prime instructions…", "the busy state ends"


def test_the_action_without_a_card_toasts_through_the_console_queue(card) -> None:
    """The palette verb runs NV_resetPrimeInstructions() with no card around it: its toast goes to window.primerApi.toastPush."""
    card.run(f'REPLIES["POST /setup/reset_prime_instructions"] = {{ ok: {json.dumps(RESULT)} }}; window.__res = null;'
             " window.NV_resetPrimeInstructions().then(function (r) { window.__res = r; })")
    assert card.js("LOG.pushed") == [{"kind": "success", "text": "Prime instructions reset: agents.md in prime is the shipped version again.", "requestId": None}]
    assert card.js("window.__res") == RESULT


# ---- the palette verb -------------------------------------------------------------------------------------------------------------------------------------

def _verb_block() -> str:
    code = blank(SHELL, strings=True)
    at = SHELL.index('id: "setup.reset_prime_instructions"')
    start = SHELL.rindex("reg({", 0, at)
    end = close_of(code, start + len("reg("))
    return SHELL[start:end + 1]


@pytest.fixture
def verbs():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(VERBS)
    ctx.eval("var RAN = 0; window.NV_resetPrimeInstructions = function () { RAN += 1; return Promise.resolve(null); };"
             " var registry = SH_createVerbRegistry(); function reg(v) { registry.register(v); }")
    ctx.eval(_verb_block())
    try:
        yield lambda expr: json.loads(ctx.eval(f"JSON.stringify({{v: {expr}}})")).get("v")
    finally:
        ctx.close()


def _ranked(ev, ctx: str) -> list[str]:
    return ev(f'SH_rankVerbs(registry, "reset prime", {ctx}).map(function (v) {{ return v.id; }})')


def test_the_verb_is_offered_to_an_admin_only(verbs) -> None:
    assert _ranked(verbs, '{ role: "admin" }') == ["setup.reset_prime_instructions"]
    for ctx in ('{ role: "user" }', '{ role: "restricted" }', "{}"):
        assert _ranked(verbs, ctx) == [], ctx


def test_the_verb_is_destructive_has_a_pointer_surface_and_runs_the_action(verbs) -> None:
    verb = verbs('(function (v) { return { label: v.canonical, surfaces: v.surfaces, destructive: v.destructive }; })(registry.get("setup.reset_prime_instructions"))')
    assert verb == {"label": "Reset Prime Instructions", "surfaces": ["overlay-button", "palette"], "destructive": True}
    verbs('registry.get("setup.reset_prime_instructions").run()')
    assert verbs("RAN") == 1


def test_the_palette_ranks_with_the_signed_in_role() -> None:
    ranking = PALETTE[PALETTE.index("var rankedVerbs = SH_rankVerbs(con.registry, q, {"):]
    ranking = ranking[:ranking.index("});")]
    assert "role: con.role," in ranking
