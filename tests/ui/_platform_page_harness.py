"""One MiniRacer harness for the Platform page: the REAL platform kit and ``ui/components/console/nv-platform.jsx``, transpiled the way the server
bundles them, on the hook runtime of ``tests/ui/_mini_react.py`` (Platform revamp Plan 01; Task 3 builds it, later tasks reuse it).

``platform_page_context(prelude_extra="")`` returns a fresh context (the caller closes it) holding these stand-ins, set up before the code loads and
free to change afterwards:

* ``CON``: what ``NV_useConsole()`` returns. ``view`` is ``{name: "platform", nav: "agents", sub: null}`` and ``overlay`` is null; ``openOverlay``,
  ``goView``, ``closeOverlayTo`` and ``toast`` push ``[name, ...args]`` into ``CALLS``.
* ``ITEMS``, ``LOADING``, ``ERROR``, ``REFETCHES``: the answer of ``window.primerApi.useResource`` (``data`` is ``{items: ITEMS}``, or none while
  ``ITEMS`` is null; ``refetch`` counts into ``REFETCHES``). The options each key was asked with are kept in ``RESOURCE_OPTS[key]``.
* ``window.primerApi.resourceState(res)``: ``"loading"`` while there is no data and the resource is loading, else ``"ready"``.
* ``window.primerApi.apiFetch`` (each call recorded in ``FETCHES``; it resolves) and ``confirmDialog`` (each request recorded in ``DIALOGS``; it
  resolves to ``CONFIRM``), ``toastPush`` (into ``TOASTS``) and ``readRefusal``.
* ``Modal`` and ``Btn`` as minimal elements; ``NV_identity``, ``NV_GRAPH_GLYPH``, ``SH_api``, ``SH_pendingId`` and ``SH_routingLine``.
* ``SAMPLE_ROW``: one row carrying a field for every entry's card.
* ``rec(name)``, a callback that records ``[name, ...args]`` into ``CALLS``; the ``useEscape`` recorder (``__layers``) and the ``document`` listener
  stub (``DOC``) of the kit's own tests.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "ui" / "components" / "shared" / "platform-kit.jsx"
PLATFORM = ROOT / "ui" / "components" / "console" / "nv-platform.jsx"

PRELUDE = r"""
var CALLS = [];
function rec(n) { return function () { CALLS.push([n].concat([].slice.call(arguments))); }; }
var CON = {
  username: "ana", role: "admin", wid: "w1", overlay: null, view: { name: "platform", nav: "agents", sub: null },
  openOverlay: rec("openOverlay"), goView: rec("goView"), closeOverlayTo: rec("closeOverlayTo"), toast: rec("toast"),
};
function NV_useConsole() { return CON; }
var ITEMS = [], LOADING = false, ERROR = null, REFETCHES = 0, RESOURCE_OPTS = {};
var FETCHES = [], DIALOGS = [], TOASTS = [], CONFIRM = true;
var __layers = [];
var DOC = {};
var document = { addEventListener: function (t, f) { DOC[t] = f; }, removeEventListener: function (t, f) { if (DOC[t] === f) delete DOC[t]; }, body: { style: {} } };
window.primerApi = {
  useEscape: function (h, a) { __layers.push({ handler: h, active: a === undefined ? true : !!a }); },
  useFocusTrap: function () {}, useViewport: function () { return { isMobile: false }; },
  useResource: function (key, fn, opts) {
    RESOURCE_OPTS[key] = opts || null;
    return { data: ITEMS === null ? undefined : { items: ITEMS }, loading: LOADING, error: ERROR, degraded: false,
             refetch: function () { REFETCHES += 1; } };
  },
  resourceState: function (res) { return !res.data && res.loading ? "loading" : "ready"; },
  apiFetch: function (method, path, body) { FETCHES.push([method, path, body || null]); return Promise.resolve({}); },
  toastPush: function (t) { TOASTS.push(t); },
  readRefusal: function (e) { return { message: (e && (e.detail || e.message)) || "refused", code: null }; },
};
function confirmDialog(o) { DIALOGS.push(o); return Promise.resolve(CONFIRM); }
function Modal(p) { return React.createElement("div", { "data-testid": p.testid || "modal", className: "modal" }, p.children, p.footer || null); }
function Btn(p) { return React.createElement("button", Object.assign({ type: "button" }, p)); }
function NV_identity() { return { color: "var(--teal)", d: "M0 0h2" }; }
var NV_GRAPH_GLYPH = { d: "M1 1h2", color: "var(--text-3)" };
var SH_api = { approvalRecords: function () { return Promise.resolve({ items: [] }); } };
function SH_pendingId(a, b, c) { return [a, b, c].join(":"); }
function SH_routingLine() { return "anyone"; }
var SAMPLE_ROW = {
  id: "r1", name: "Row one", slug: "row-one", description: "A row", tagline: "A row", status: "ok", state: "running", enabled: true,
  available: true, builtin: false, kind: "mcp", system: false, provider_id: "p1", model_name: "m1", context_length: 8192,
  config: { reasoning: "high" }, model_profile_id: "prof-1", tools: ["a", "b"], nodes: [1, 2], edges: [1], template_id: "t1",
  backend: { kind: "docker", image: "img:1" }, documents: [1, 2, 3], search: true, schedule: "* * * * *", source: "git", workspace_id: "w1",
  agent_id: "a1", active_version: "3", toolset_id: "web", tool_name: "fetch", approval: { type: "llm", provider_id: "p1", model: "m1" },
  approvers: { kind: "anyone" }, timeout_seconds: 600,
};
"""


@cache
def _code() -> str:
    """The kit then the page, in the order of ``ui/index.html`` (the kit loads first)."""
    return transpile(KIT) + "\n" + transpile(PLATFORM)


def platform_page_context(prelude_extra: str = ""):
    """A fresh V8 context: the hook runtime, the stand-ins above (plus ``prelude_extra``), then the real kit and Platform page. The caller closes it."""
    return mini_react_context(_code(), PRELUDE + prelude_extra)


def platform_navs() -> list[str]:
    """Every key of ``NV_PLAT_PAGES``, read from the source (an entry is a line ``  <id>: {`` inside the table)."""
    src = PLATFORM.read_text(encoding="utf-8")
    body = src[src.index("var NV_PLAT_PAGES = {"):]
    body = body[: body.index("\n};")]
    return re.findall(r"^  (\w+): \{$", body, re.M)
