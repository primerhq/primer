"""The agent form (``AG_NewAgentModal``, ``ui/components/agents.jsx``) mounted in V8, for tests that drive it and read what it sends.

``agent_form()`` opens a MiniRacer context with the hook runtime of ``tests/ui/_mini_react.py``, the stubs below, and then the real
``ui/foundation/use-mutation.js``, ``ui/components/shared/form-field.jsx`` and ``ui/components/agents.jsx`` as the server's bundler emits them.
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
* ``window.addEventListener`` and ``window.removeEventListener`` add to and remove from ``__listeners``.

A test mounts the form (``mount``), drives it (``type_into``, ``click``) and reads what it sent (``calls``, ``body``).
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

_FILES = ("foundation/use-mutation.js", "components/shared/form-field.jsx", "components/agents.jsx")

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
