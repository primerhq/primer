"""The console's built-in toolset ids follow the catalogue (plan 06 Task 3).

``TS_BUILTIN_RESERVED_IDS`` is the console's copy of
``primer.api.routers.providers._BUILTIN_TOOLSETS`` (the catalogue), and the
New toolset form's hint names those ids. A JS copy pinned to the server list
is the pattern the repo already uses (as ``AG_validateNewAgent`` does), so
the ids and the hint text are read back out of the transpiled file and
checked against the server list, not against a second hand-written list.
"""

from __future__ import annotations

import json
from pathlib import Path

from py_mini_racer import MiniRacer

from tests.ui._mini_react import mini_react_context, transpile

TOOLSETS = Path(__file__).resolve().parents[2] / "ui" / "components" / "toolsets.jsx"
SOURCE = TOOLSETS.read_text(encoding="utf-8")

_PRELUDE = """
var __calls = []; var __created = []; var __closed = 0; var __toasts = [];
globalThis.Btn = function (props) { return React.createElement("button", props, props.children); };
globalThis.Icon = function () { return null; };
globalThis.Banner = function () { return null; };
globalThis.Modal = function (props) { return React.createElement("div", { "data-testid": "modal" }, props.children, props.footer); };
// FormField is the labelled row the form draws its fields with (console review C-003); these cases drive the form by test id, so a pass-through row is all they need.
globalThis.FormField = function (props) { return React.createElement("div", null, props.children); };
window.primerApi = {
  apiFetch: function (method, path, body) {
    __calls.push(method + " " + path);
    if (method === "POST" && path.indexOf("/toolsets") === 0) {
      return Promise.resolve({ id: body.id || "ts-new", provider: body.provider, config: body.config });
    }
    if (method === "PUT" && path.indexOf("/toolsets/") === 0) return Promise.resolve({ id: path.split("/")[2], provider: body.provider, config: body.config });
    if (method === "GET" && /\\/toolsets\\/[^/]+\\/tools$/.test(path)) return Promise.resolve({ tools: [{ id: "a" }, { id: "b" }] });
    return Promise.resolve({});
  },
  useMutation: function (fetcher, opts) {
    return {
      loading: false,
      mutate: function (arg) {
        return Promise.resolve(fetcher(arg)).then(function (d) { if (opts.onSuccess) opts.onSuccess(d); return d; },
                                                  function (e) { if (opts.onError) opts.onError(e); throw e; });
      },
    };
  },
};
"""

_TEXT = """
function __text(n) {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(__text).join("");
  if (n.__el) return __text(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  return "";
}
"""


def _console_ids() -> list[str]:
    start = SOURCE.index("const TS_BUILTIN_RESERVED_IDS")
    ctx = MiniRacer()
    try:
        ctx.eval(SOURCE[start:SOURCE.index(";", start) + 1])
        return json.loads(ctx.eval("JSON.stringify(TS_BUILTIN_RESERVED_IDS)"))
    finally:
        ctx.close()


def _hint_text() -> str:
    ctx = mini_react_context(transpile(TOOLSETS), _PRELUDE)
    try:
        ctx.eval(_TEXT)
        ctx.eval("MR.mount(TS_NewToolsetModal, { onClose: function () {}, onCreate: function () {}, pushToast: function () {} });")
        return ctx.eval("__text(MR.find('toolset-builtin-hint'))")
    finally:
        ctx.close()


def test_the_console_built_in_ids_are_the_server_catalogue() -> None:
    from primer.api.routers import providers

    assert _console_ids() == [spec["id"] for spec in providers._BUILTIN_TOOLSETS]


def test_the_console_names_no_search_toolset() -> None:
    assert "search" not in _console_ids()


def test_the_new_toolset_hint_names_every_built_in_toolset() -> None:
    text = _hint_text()
    assert text == ("Internal toolsets (system, workspaces, collections, misc, web, harness, trigger, workspace_ext, crud, canvas) "
                    "are runtime built-ins: they cannot be created via this form.")
    assert "\u2014" not in text
