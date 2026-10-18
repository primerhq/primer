"""The agent form (``AG_NewAgentModal``, ``ui/components/agents.jsx``) mounted in V8, for tests that drive it and read what it sends.

``agent_form()`` opens a MiniRacer context with the hook runtime of ``tests/ui/_mini_react.py``, the stubs below, and then the real
``ui/foundation/escape-stack.js``, ``ui/foundation/use-mutation.js``, ``ui/components/shared/form-field.jsx`` and ``ui/components/agents.jsx`` as the
server's bundler emits them.
It closes the context on exit: an isolate that is never closed stays in memory for the rest of the run.

The stubs stand in for what the console around the form provides:

* ``Icon`` and ``StatusPill`` render nothing, and ``relativeTime`` says "now".
* ``Btn``, ``Banner`` and ``Modal`` render plain elements that keep the ``data-testid``. ``Btn`` also keeps ``disabled``, and ``busy`` as ``data-busy``.
* ``window.ToolPicker`` is one button, ``tool-picker-stub``. Its ``data-selected`` is the sorted selection as JSON, and a click selects
  ``web__search`` and ``system__list_files``.
* ``window.primerApi.useResource`` answers the model profiles (``PROFILES``) and the voices. Any other key has no data.
* ``useCapabilities`` reports text to speech as set up or not (``agent_form(tts_configured=...)``), and ``useRouter`` navigates nowhere.
* ``apiFetch`` records each call and resolves with the body plus an id. The body is recorded as JSON, which is what the wire carries.
  ``useMutation`` is the real one.
* ``window.addEventListener`` and ``window.removeEventListener`` add to and remove from ``__listeners``; the real Escape stack registers its one
  ``keydown`` listener there, and ``press_escape`` answers it.

A test mounts the form (``mount``), drives it (``type_into``, ``click``) and reads what it sent (``calls``, ``body``). It reads what the form drew
with ``prop``, ``text_of`` and ``testids_under``. ``prop`` reads the element a ``Btn`` RENDERS (a ``data-busy`` is on the ``<button>``, not on the ``Btn``
element ``MR.find`` answers first). ``mount_prompt_tabs`` mounts the system prompt editor alone, under a host that keeps its parts and closes on Escape.
"""

from __future__ import annotations

import contextlib
import functools
import json
from collections.abc import Iterator
from typing import Any

from tests.ui._mini_react import ROOT, mini_react_context, transpile

PROFILES = [{"id": "fast", "provider_id": "p1", "model_name": "m1"}, {"id": "slow", "provider_id": "p1", "model_name": "m2"}]
STATUS_OK = {"data": {"ok": True, "issues": [], "warnings": []}, "loading": False, "error": None}

# An agent row with every field set, as GET /v1/agents/<id> returns it.
FULL = {
    "id": "refund-triage", "description": "Triages refunds", "model": {"profile_id": "slow"},
    "temperature": 0.7, "max_tool_turns": 25, "max_output_tokens": 4096,
    "tools": ["system__list_files", "web__search"],
    "system_prompt": ["You triage refunds.", "Be brief."], "compaction_prompt": ["Keep the cited sources."],
    "compaction_tool_access": True, "allow_external_tools": True,
    "response_format": {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]},
    "tts_voice": "alloy", "harness_id": None,
}
# An agent row that keeps every default of the model.
DEFAULTS = {
    "id": "plain", "description": "Plain", "model": {"profile_id": "fast"}, "temperature": None, "max_tool_turns": 50,
    "max_output_tokens": None, "tools": [], "system_prompt": [], "compaction_prompt": [], "compaction_tool_access": False,
    "allow_external_tools": False, "response_format": None, "tts_voice": None, "harness_id": None,
}
# A row a harness manages: the form shows a locked summary instead of inputs.
MANAGED = {**FULL, "harness_id": "h1"}

_FILES = ("foundation/escape-stack.js", "foundation/use-mutation.js", "components/shared/form-field.jsx", "components/agents.jsx")

_STUBS = r"""
var __calls = [];
var __recorded = { closes: 0, created: [], toasts: [], deletes: 0, chats: 0 };
var __listeners = [];
var __resources = {
  "agents:model-profiles": { data: { items: PROFILES }, loading: false, error: null },
  "agents:voices": { data: { voices: ["alloy", "nova"] }, loading: false },
};
window.addEventListener = function (type, fn) { __listeners.push({ type: type, fn: fn }); };
window.removeEventListener = function (type, fn) {
  __listeners = __listeners.filter(function (l) { return !(l.type === type && l.fn === fn); });
};
function Icon() { return null; }
function StatusPill() { return null; }
function relativeTime() { return "now"; }
function Btn(p) {
  return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, "data-busy": p.busy ? "true" : "false", onClick: p.onClick }, p.children);
}
function Banner(p) { return React.createElement("div", { "data-testid": p["data-testid"] }, p.title, p.detail, p.children); }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.title, p.children, p.footer); }
window.ToolPicker = function (p) {
  return React.createElement("button", {
    "data-testid": "tool-picker-stub",
    "data-selected": JSON.stringify(Array.from(p.selected || []).sort()),
    onClick: function () { p.onChange(new Set(["web__search", "system__list_files"])); },
  });
};
window.primerApi = {
  useResource: function (key) {
    if (Object.prototype.hasOwnProperty.call(__resources, key)) return __resources[key];
    return { data: null, loading: false, error: null, refetch: function () {} };
  },
  useCapabilities: function () { return { data: { speech: { tts_configured: TTS_CONFIGURED } } }; },
  useRouter: function () { return { navigate: function () {}, params: {} }; },
  apiFetch: function (method, path, body) {
    __calls.push({ method: method, path: path, body: body === undefined ? null : JSON.parse(JSON.stringify(body)) });
    return Promise.resolve(Object.assign({ id: "agent-new" }, body));
  },
};
function __mount(existing) {
  var props = {
    onClose: function () { __recorded.closes++; },
    onCreate: function (row) { __recorded.created.push(row); },
    pushToast: function (t) { __recorded.toasts.push(t); },
  };
  if (existing) {
    // As AgentDetail passes them.
    props.existing = existing;
    props.status = STATUS_OK;
    props.onDelete = function () { __recorded.deletes++; };
    props.onChat = function () { __recorded.chats++; };
    props.chatLoading = false;
  }
  MR.mount(AG_NewAgentModal, props);
}
function __typeInto(testid, value) {
  var el = MR.find(testid);
  if (!el) throw new Error("no element with data-testid " + testid);
  el.props.onChange({ target: { value: value } });
  MR.rerender();
}
// Every element with exactly this test id, in tree order. A component that takes a data-testid prop (Btn) is found before the element it renders.
function __withTestid(testid) {
  var all = MR.findAll(testid).filter(function (el) { return el.props["data-testid"] === testid; });
  if (!all.length) throw new Error("no element with data-testid " + testid);
  return all;
}
// The element that DRAWS the test id: the host element (a <button>) before the component (Btn) that rendered it. A prop is read from the first of them
// that carries it, so a data-busy (only on the <button>) and an aria-pressed a Btn forwards (only on the Btn element, in this harness) are both found.
function __prop(testid, name) {
  var all = __withTestid(testid);
  var hosts = all.filter(function (el) { return typeof el.type === "string"; });
  var comps = all.filter(function (el) { return typeof el.type !== "string"; });
  var order = hosts.concat(comps);
  for (var i = 0; i < order.length; i++) {
    if (Object.prototype.hasOwnProperty.call(order[i].props, name)) return order[i].props[name];
  }
  return undefined;
}
function __textOf(testid) {
  var all = __withTestid(testid);
  var el = all.filter(function (e) { return typeof e.type === "string"; })[0] || all[0];
  var out = [];
  (function collect(n) {
    if (n == null || typeof n === "boolean") return;
    if (typeof n === "string" || typeof n === "number") { out.push(String(n)); return; }
    if (Array.isArray(n)) { n.forEach(collect); return; }
    if (n.__el) collect(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  })(el);
  return out.join("");
}
function __pressEscape() {
  __listeners.filter(function (l) { return l.type === "keydown"; }).forEach(function (l) {
    l.fn({ key: "Escape", defaultPrevented: false, isComposing: false, keyCode: 27 });
  });
  MR.rerender();
}
// The system prompt editor alone, under a host that keeps its parts in state (the overlay does) and registers a close on Escape BEFORE it renders
// the editor, as the overlay's Modal does: the editor's own Escape layer sits above it.
var __hostParts = [], __hostSetParts = null, __initialParts = [""];
function __PromptHost() {
  var st = React.useState(__initialParts);
  __hostParts = st[0];
  __hostSetParts = st[1];
  window.primerApi.useEscape(function () { __recorded.closes++; });
  return React.createElement(AG_PromptTabs, { parts: st[0], onChange: st[1] });
}
function __mountPromptTabs(parts) {
  __initialParts = parts;
  MR.mount(__PromptHost, {});
}
function __setParts(parts) {
  __hostSetParts(parts);
  MR.rerender();
}
"""


@functools.lru_cache(maxsize=1)
def _code() -> str:
    return "\n;\n".join(transpile(ROOT / "ui" / rel) for rel in _FILES)


def _prelude(tts_configured: bool) -> str:
    return (
        f"var PROFILES = {json.dumps(PROFILES)};\nvar STATUS_OK = {json.dumps(STATUS_OK)};\n"
        f"var TTS_CONFIGURED = {json.dumps(tts_configured)};\n" + _STUBS
    )


@contextlib.contextmanager
def agent_form(*, tts_configured: bool = True) -> Iterator[Any]:
    """A fresh context holding the form, closed on exit."""
    ctx = mini_react_context(_code(), _prelude(tts_configured))
    try:
        yield ctx
    finally:
        ctx.close()


def mount(ctx: Any, existing: dict | None = None) -> None:
    """Mount ``AG_NewAgentModal``: the create form, or with ``existing`` the edit form, with the props ``AgentDetail`` passes."""
    ctx.eval(f"__mount({json.dumps(existing)})")


def type_into(ctx: Any, testid: str, value: str) -> None:
    """Call the element's ``onChange({target: {value}})`` and re-render. Throws ``no element with data-testid <testid>`` when there is none."""
    ctx.eval(f"__typeInto({json.dumps(testid)}, {json.dumps(value)})")


def click(ctx: Any, testid: str) -> None:
    """``MR.click``, then a re-render in a SECOND eval, so the promise callbacks the click queued (a save's response) have run."""
    ctx.eval(f"MR.click({json.dumps(testid)})")
    ctx.eval("MR.rerender()")


def calls(ctx: Any) -> list[dict]:
    """The ``apiFetch`` calls in order, each ``{"method", "path", "body"}``."""
    return json.loads(ctx.eval("JSON.stringify(__calls)"))


def body(ctx: Any) -> dict:
    """The body of the one ``apiFetch`` call the form made."""
    made = calls(ctx)
    assert len(made) == 1, f"expected exactly one apiFetch call, got {made}"
    return made[0]["body"]


def prop(ctx: Any, testid: str, name: str) -> Any:
    """One prop of the element that draws ``testid``, JSON-decoded (None when it has no such prop). Throws when there is no such element.

    A ``Btn`` is a component that renders a ``<button>``: ``MR.find`` answers the ``Btn`` element first, and its props are not the button's
    (``busy`` there, ``data-busy`` on the button). This reads the rendered ``<button>`` first, then the ``Btn`` element for a prop only it has.
    """
    return json.loads(ctx.eval(f"JSON.stringify({{v: __prop({json.dumps(testid)}, {json.dumps(name)})}})")).get("v")


def text_of(ctx: Any, testid: str) -> str:
    """The text under the element that draws ``testid``, joined with no separator."""
    return ctx.eval(f"__textOf({json.dumps(testid)})")


def testids_under(ctx: Any, testid: str) -> list[str]:
    """The test ids of the elements under the one with ``testid`` (not itself), in tree order. A component that carries a ``data-testid`` (``Btn``)
    and the element it renders are one, not two."""
    tree = json.loads(ctx.eval(f"JSON.stringify(MR.subtree({json.dumps(testid)}))"))
    assert tree is not None, f"no element with data-testid {testid}"
    ids = [e["testid"] for e in tree if e["type"] != "component" and isinstance(e.get("testid"), str)]
    return ids[1:] if ids and ids[0] == testid else ids


testids_under.__test__ = False  # a test module imports it, and pytest collects every name that starts with "test"


def press_escape(ctx: Any) -> None:
    """Answer Escape as the window does: call every ``keydown`` listener registered through ``window.addEventListener``, then re-render."""
    ctx.eval("__pressEscape()")


def mount_prompt_tabs(ctx: Any, parts: list[str]) -> None:
    """Mount ``AG_PromptTabs`` alone under a host that keeps ``parts`` and counts how often Escape closed it (``closes``)."""
    ctx.eval(f"__mountPromptTabs({json.dumps(parts)})")


def parts(ctx: Any) -> list[str]:
    """The parts the host holds now (what the editor last reported)."""
    return json.loads(ctx.eval("JSON.stringify(__hostParts)"))


def set_parts(ctx: Any, new_parts: list[str]) -> None:
    """Replace the host's parts from outside the editor, then re-render."""
    ctx.eval(f"__setParts({json.dumps(new_parts)})")


def closes(ctx: Any) -> int:
    """How many times Escape reached the host's close handler (the overlay's Modal, in the real form)."""
    return int(ctx.eval("__recorded.closes"))
