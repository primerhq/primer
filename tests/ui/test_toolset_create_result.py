"""Creating a Python toolset does not claim a connection (finding ADM-11 of the 2026-10-08 admin review).

After a create, ``TS_NewToolsetModal`` swapped the form for ``TS_ConnectResult`` for EVERY provider: "Registered <id> ... checking the connection", a spinner
saying "Connecting to the MCP server and importing tools", then "Connected. 0 tools imported." A Python toolset has no connection (its tools are the functions
in the source the operator writes AFTER the create), so the panel said something false and gave no hint that the code comes next. The probe it ran
(``GET /toolsets/<id>/tools``) is meaningful for an MCP server only.

A Python create now says what happened and what comes next, runs no probe, and its Done button is "Open the editor" (the toolset's detail opens on the Config
tab, which for a Python toolset IS the editor). An MCP create is unchanged.

The REAL ``TS_NewToolsetModal`` (``toolsets.jsx`` transpiled the way the server bundles it) runs in the V8 stand-in for React; ``tests/ui_e2e/
test_platform_create_opens_the_form_journey.py`` drives the same flow in a browser.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

TOOLSETS = Path(__file__).resolve().parents[2] / "ui" / "components" / "toolsets.jsx"

_PRELUDE = """
var __calls = []; var __created = []; var __closed = 0; var __toasts = [];
globalThis.Btn = function (props) { return React.createElement("button", props, props.children); };
globalThis.Icon = function () { return null; };
globalThis.Banner = function () { return null; };
globalThis.Modal = function (props) { return React.createElement("div", { "data-testid": "modal" }, props.children, props.footer); };
window.primerApi = {
  apiFetch: function (method, path, body) {
    __calls.push(method + " " + path);
    if (method === "POST" && path.indexOf("/toolsets") === 0) {
      return Promise.resolve({ id: body.id || "ts-new", provider: body.provider, config: body.config });
    }
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
  if (n.__el) return __text(typeof n.type === "function" ? n.out : n.children);
  return "";
}
function __shown() { return __text(MR.find("modal")); }
function __settle() { for (var i = 0; i < 6; i++) MR.rerender(); }
"""


@pytest.fixture
def modal():
    ctx = mini_react_context(transpile(TOOLSETS), _PRELUDE)
    ctx.eval(_TEXT)
    ctx.eval(
        "MR.mount(TS_NewToolsetModal, { onClose: function () { __closed++; }, onCreate: function (row) { __created.push(row.id); },"
        " pushToast: function (t) { __toasts.push(t); } });"
    )
    yield ctx
    ctx.close()


def _settle(ctx, rounds: int = 6) -> None:
    """Promise continuations run between evals in MiniRacer, so draw, let them run, draw again."""
    for _ in range(rounds):
        ctx.eval("MR.rerender()")


def _choose(ctx, provider: str, id_: str = "") -> None:
    if id_:
        ctx.eval(f"MR.find('toolset-id-input').props.onChange({{ target: {{ value: {json.dumps(id_)} }} }})")
    ctx.eval(f"MR.find('toolset-provider-select').props.onChange({{ target: {{ value: {json.dumps(provider)} }} }})")
    _settle(ctx, 2)


def _create(ctx) -> None:
    ctx.eval("MR.click('toolset-create-btn')")
    _settle(ctx)


def _calls(ctx) -> list[str]:
    return json.loads(ctx.eval("JSON.stringify(__calls)"))


# ---- a Python toolset has no connection ---------------------------------------------------------------------------------------------------------------------


def test_a_python_create_says_what_was_created_and_what_comes_next(modal) -> None:
    _choose(modal, "python", "my-tools")
    _create(modal)

    shown = modal.eval("__shown()")
    assert "Created" in shown and "my-tools" in shown, shown
    assert "no connection to check" in shown, shown
    assert "write its source" in shown and "editor" in shown, shown


def test_a_python_create_does_not_claim_to_connect(modal) -> None:
    _choose(modal, "python", "my-tools")
    _create(modal)

    shown = modal.eval("__shown()")
    for claim in ("Connected", "Connecting", "checking the connection", "tools imported", "MCP server"):
        assert claim not in shown, f"{claim!r} on a Python toolset's result: {shown!r}"


def test_a_python_create_runs_no_connection_probe(modal) -> None:
    _choose(modal, "python", "my-tools")
    _create(modal)

    calls = _calls(modal)
    assert "POST /toolsets" in calls
    assert not [c for c in calls if c.startswith("GET /toolsets/") and c.endswith("/tools")], f"a probe ran for a toolset that has no connection: {calls}"


def test_a_python_creates_done_button_opens_the_editor_and_is_ready_at_once(modal) -> None:
    _choose(modal, "python", "my-tools")
    _create(modal)

    done = json.loads(modal.eval("JSON.stringify((function () { var d = MR.find('toolset-connect-done'); return { label: __text(d), disabled: !!d.props.disabled }; })())"))
    assert done == {"label": "Open the editor", "disabled": False}, done
    modal.eval("MR.click('toolset-connect-done')")
    _settle(modal, 2)
    assert json.loads(modal.eval("JSON.stringify(__created)")) == ["my-tools"], "Done hands the created row on, which opens its detail (the editor)"


# ---- an MCP create is unchanged -----------------------------------------------------------------------------------------------------------------------------


def test_an_mcp_create_still_probes_and_reports_the_connection(modal) -> None:
    _choose(modal, "mcp", "gh")
    modal.eval("MR.find('toolset-command-input').props.onChange({ target: { value: 'npx @modelcontextprotocol/server-github' } })")
    _settle(modal, 2)
    _create(modal)

    assert "GET /toolsets/gh/tools" in _calls(modal), "an MCP server has a connection and it is checked"
    shown = modal.eval("__shown()")
    assert "checking the connection" in shown and "Connected." in shown and "2 tools imported" in shown, shown
    done = modal.eval("__text(MR.find('toolset-connect-done'))")
    assert done == "Done", done
