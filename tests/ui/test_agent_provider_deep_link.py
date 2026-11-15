"""The agent detail's provider deep link navigates the providers overlay's path form (final review fix wave, B2; fix round 1).

The first cut navigated with `navigate("/providers", { class, id })`: `SH_shimNavigate` (ui/foundation/shell-router-shim.js)
reads only the path segments and the `tab` / `workspace_id` options, so the options object opened nothing (the old call was
inert too). The providers overlay's grammar is the path (NV_OVERLAY_MOUNTS.providers in ui/components/console/nv-overlays.jsx:
the class off the section segment, the instance off the record segment), and the catalog's own instance navigation writes that
same two-segment form (`shell.openOverlay("providers", classKey, id)`). The class comes from the class table (`PC_classOf`);
while the provider row is still loading (or errored) the click navigates nowhere, because a guessed class would open a page a
claude_code row is not on. The test drives the REAL panel (`AG_ReferencesPanel`, agents.jsx) in V8: the hooks are
stubbed, the class table is the real one (register-common.jsx, as the bundle loads it before agents.jsx), the link
is clicked, and the exact `navigate`
argument is asserted -- a source-text pin passes on the inert call too, so it proves nothing. The sibling "Model
profile" link (agents.jsx) still navigates `navigate("/providers", { class: "llm" })`: pre-existing, out of scope, left as-is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import ROOT, mini_react_context, transpile
from tests.ui._register_harness import base_code, context, js as reg_js

AGENTS = ROOT / "ui" / "components" / "agents.jsx"

# The agent the panel is handed: one model profile, one provider hop out.
AGENT = {"id": "ag-1", "model": {"profile_id": "pf-1"}, "tools": []}

PRELUDE = r"""
var RES = {}, NAVS = [];
window.primerApi = {
  useResource: function (key, fetcher, opts) {
    if (!RES[key]) RES[key] = { data: undefined, error: null, loading: true };
    return RES[key];
  },
  useRouter: function () {
    return { params: {}, navigate: function (path, opts) { NAVS.push([path, opts === undefined ? null : opts]); } };
  },
  useViewport: function () { return { isMobile: false }; },
  apiFetch: function () { return Promise.resolve(null); },
};
function Icon() { return null; }
"""

AFTER = r"""
function serve(key, row) { RES[key] = { data: row, error: null, loading: row === undefined }; }
function mountPanel(agent) {
  NAVS = [];
  MR.mount(function Nothing() { return null; }, {});
  MR.mount(AG_ReferencesPanel, { agent: agent });
}
function clickProviderLink() {
  var el = MR.find("ag-ref-provider-link");
  if (!el) throw new Error("no element with data-testid ag-ref-provider-link");
  el.props.onClick();
  MR.rerender();
}
"""


@pytest.fixture(scope="module")
def agents_code() -> str:
    # as ui/index.html loads it: the class table (register-common.jsx) before the page that reads it
    return "\n;\n".join([
        transpile(ROOT / "ui" / "components" / "providers" / "register-common.jsx"),
        transpile(AGENTS),
    ])


@pytest.fixture
def ctx(agents_code):
    c = mini_react_context(agents_code, PRELUDE)
    c.eval(AFTER)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    """The value of a JS expression as Python data; ``undefined`` is ``None``."""
    return json.loads(ctx.eval(f"(function () {{ var v = ({expr}); return JSON.stringify(v === undefined ? null : v); }})()"))


@pytest.fixture(scope="module")
def reg_code() -> str:
    return base_code()


@pytest.fixture
def reg_ctx(reg_code):
    c = context(reg_code)
    try:
        yield c
    finally:
        c.close()


def test_the_class_table_resolves_the_class_of_a_provider_row(reg_ctx) -> None:
    assert reg_js(reg_ctx, 'PC_classOf("llm_providers", { provider: "claude_code" })') == "agent_runtime", \
        "a claude_code provider is an agent runtime"
    assert reg_js(reg_ctx, 'PC_classOf("llm_providers", { provider: "anthropic" })') == "llm"
    assert reg_js(reg_ctx, 'PC_classOf("llm_providers", { provider: null })') == "llm", "a row without a kind the rules on is the first class'"
    assert reg_js(reg_ctx, 'PC_classOf("embedding_providers", { provider: "openai" })') == "embedding"


def test_the_provider_deep_link_navigates_the_path_form_of_the_providers_overlay(ctx) -> None:
    # the claude_code row: the path form, the class resolved from the row's kind, the id the record
    ctx.eval(f"serve('model-profile:pf-1', {json.dumps({'id': 'pf-1', 'provider_id': 'cc-lp'})});")
    ctx.eval(f"serve('llm-provider:cc-lp', {json.dumps({'id': 'cc-lp', 'provider': 'claude_code'})});")
    ctx.eval(f"mountPanel({json.dumps(AGENT)});")
    ctx.eval("clickProviderLink();")
    assert js(ctx, "NAVS") == [["/providers/agent_runtime/cc-lp", None]], \
        f"the link navigates /providers/<class>/<id> with the resolved class: {js(ctx, 'NAVS')}"

    # while the provider row is still loading the click navigates nowhere (no guessed class)
    ctx.eval("serve('llm-provider:cc-lp', null);")
    ctx.eval(f"mountPanel({json.dumps(AGENT)});")
    ctx.eval("clickProviderLink();")
    assert js(ctx, "NAVS") == [], "no navigate call while provider.data is absent"

    # a non-claude_code row stays under the llm class
    ctx.eval(f"serve('model-profile:pf-1', {json.dumps({'id': 'pf-1', 'provider_id': 'other'})});")
    ctx.eval(f"serve('llm-provider:other', {json.dumps({'id': 'other', 'provider': 'anthropic'})});")
    ctx.eval(f"mountPanel({json.dumps(AGENT)});")
    ctx.eval("clickProviderLink();")
    assert js(ctx, "NAVS") == [["/providers/llm/other", None]]
