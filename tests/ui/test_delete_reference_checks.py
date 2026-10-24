"""What the delete dialogs of a semantic search provider and a workspace provider say about what refers to them (#923, round 2).

Each dialog reads a whole list (``PR_fetchAllPages``) and filters what refers to the entity. Round 2 of the review found the words false in two places:
the read cut short said "the server refuses the delete if one of them does", but

* the server deletes an SSP whatever points at it (``primer/api/routers/semantic_search.py``): a collection that still points at it keeps the reference and its
  semantic search fails, and the SSP's own check filtered on a field collections do not carry (``search_provider_id``; the reference is
  ``search.vector_store_provider_id``, ``primer/model/collection.py``), so it never found one;
* the server deletes a template that workspaces were made from (204) and each keeps its own copy (``tests/api/test_workspace_template_in_use.py``); the old
  template detail page whose dialog said otherwise is gone from main (73461ffc4), and ``WT_deleteTemplate``'s prompt says what the delete does;
* only a workspace provider is refused while a template uses it (``primer/api/routers/workspaces.py``), and its sentence stays.

And while the reference read was still out, the workspace-provider dialog said "Deletion is safe". The REAL components run here on the
hook runtime after the REAL ``api.js``, with the resources they read answered from ``DATA`` (the longest key prefix that matches: a value, or ``LOADING``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")
SSP = UI / "components" / "semantic-search.jsx"
SHARED = UI / "components" / "workspaces" / "shared.jsx"
PROVIDERS = UI / "components" / "workspaces" / "providers.jsx"

_PRELUDE = API + r"""
var LOADING = { __loading: true };
var DATA = {};
var CALLS = [];
window.primerApi.useResource = function (key) {
  var hit = null, best = -1;
  Object.keys(DATA).forEach(function (prefix) { if (key.indexOf(prefix) === 0 && prefix.length > best) { best = prefix.length; hit = DATA[prefix]; } });
  if (hit === LOADING || hit === null) return { data: undefined, loading: true, error: null, degraded: false, refetch: function () {} };
  return { data: hit, loading: false, error: null, degraded: false, refetch: function () {} };
};
window.primerApi.resourceState = function (r) { return r.data != null ? "ready" : (r.degraded ? "stuck" : "loading"); };
window.primerApi.useMutation = function (fn) { return { loading: false, mutate: function () { CALLS.push("mutate"); return Promise.resolve(); } }; };
window.primerApi.useRouter = function () { return { params: {}, query: {}, navigate: function () {} }; };
window.primerApi.useViewport = function () { return { isMobile: false }; };
window.primerApi.useCapabilities = function () { return { data: { extras: {} } }; };
window.primerApi.extraInstalled = function () { return true; };
window.primerApi.capabilityHint = function (x) { return String(x); };
window.primerApi.apiFetch = function () { return Promise.resolve({}); };
var document = { title: "", addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
window.addEventListener = function () {}; window.removeEventListener = function () {};
globalThis.Btn = function (p) { return React.createElement("button", { "data-testid": p["data-testid"], onClick: p.onClick, disabled: p.disabled, "data-kind": p.kind }, p.children); };
globalThis.Modal = function (p) { return React.createElement("div", { "data-testid": "modal" }, p.children, React.createElement("div", { "data-testid": "modal-footer" }, p.footer)); };
globalThis.Banner = function (p) { return React.createElement("div", { "data-testid": "banner" }, p.title, p.detail); };
globalThis.Icon = function () { return null; };
globalThis.FormField = function (p) { return React.createElement("div", null, p.children); };
globalThis.PK_Section = function (p) { return React.createElement("section", { "data-testid": p.testid }, p.children); };
window.WorkspaceBackendBadge = function () { return null; };
window.PC_isBuiltin = function () { return false; };
function textOf(n) {
  var out = [];
  (function walk(x) {
    if (x == null || typeof x === "boolean") return;
    if (typeof x === "string" || typeof x === "number") { out.push(String(x)); return; }
    if (Array.isArray(x)) { x.forEach(walk); return; }
    if (x.__el) walk(typeof x.type === "function" && x.type !== React.Fragment ? x.out : x.children);
  })(n);
  return out.join("");
}
function dangerButton() {
  var footer = MR.find("modal-footer");
  var hit = null;
  (function walk(x) {
    if (hit || x == null || typeof x !== "object") return;
    if (Array.isArray(x)) { x.forEach(walk); return; }
    if (x.__el && x.type === "button" && x.props["data-kind"] === "danger") { hit = x; return; }
    if (x.__el) walk(typeof x.type === "function" && x.type !== React.Fragment ? x.out : x.children);
  })(footer);
  return hit;
}
"""


def _ctx(*files: Path):
    return mini_react_context("\n".join(transpile(f) for f in files), _PRELUDE)


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _rows(prefix: str, n: int, extra: str = "{}") -> str:
    return f"(function () {{ var a = []; for (var i = 0; i < {n}; i++) a.push(Object.assign({{ id: '{prefix}-' + i }}, {extra})); return a; }})()"


# ---- the semantic search provider ------------------------------------------------------------------------------------------------------------------------

SSP_ROW = {"id": "pg-1", "provider": "pgvector", "config": {"db_schema": "vec"}}


@pytest.fixture
def ssp():
    c = _ctx(SSP)
    c.eval(f"DATA['ssp-detail:pg-1'] = {json.dumps(SSP_ROW)}; DATA['ssp-detail:pg-1:collections'] = LOADING;")
    try:
        yield c
    finally:
        c.close()


def _open_ssp(ctx) -> None:
    ctx.eval("MR.mount(SSPDetail, { sspId: 'pg-1' }); MR.click('ssp-detail-delete');")


def test_the_ssp_check_finds_a_collection_by_the_reference_it_carries(ssp):
    """A collection points at its vector store with search.vector_store_provider_id; the check filtered on search_provider_id and never found one."""
    ssp.eval("DATA['ssp-detail:pg-1:collections'] = { items: [{ id: 'docs', search: { vector_store_provider_id: 'pg-1', embedder: {} } },"
             " { id: 'notes', search: null }], truncated: false };")
    _open_ssp(ssp)
    text = ssp.eval("textOf(MR.find('ssp-delete-collections-in-use'))")
    assert "docs" in text and "notes" not in text
    assert "Their semantic search fails once it is deleted." in text and "409" not in text
    assert js(ssp, "dangerButton().props.disabled") is True


def test_a_cut_short_ssp_check_says_what_the_server_does(ssp):
    ssp.eval("DATA['ssp-detail:pg-1:collections'] = { items: [{ id: 'docs', search: null }], truncated: true, serverTotal: 12000, error: null };")
    _open_ssp(ssp)
    text = ssp.eval("textOf(MR.find('ssp-delete-collections-partial'))")
    assert text == ("Showing the first 1 of 12000 collections."
                    "None of these use this provider. The rest were not checked: a collection among them that does keeps pointing at it once it is"
                    " deleted, and its semantic search then fails.")
    assert "refuses" not in text and "Deletion is safe" not in ssp.eval("textOf(MR.find('modal'))")


def test_the_ssp_check_still_loading_says_it_is_checking(ssp):
    _open_ssp(ssp)
    assert ssp.eval("textOf(MR.find('ssp-delete-collections-loading'))") == "Checking for collections that reference this provider…"
    assert "Deletion is safe" not in ssp.eval("textOf(MR.find('modal'))")
    assert js(ssp, "dangerButton().props.disabled") is True


# ---- the workspace provider: the server does refuse, and the dialog waits for its check ---------------------------------------------------------------

PROVIDER_ROW = {"id": "dock", "provider": "docker", "config": {}}


@pytest.fixture
def provider():
    c = _ctx(SHARED, PROVIDERS)
    c.eval(f"DATA['ws:provider:dock:templates'] = LOADING; DATA['ws:provider:dock'] = {json.dumps(PROVIDER_ROW)};")
    try:
        yield c
    finally:
        c.close()


def test_the_provider_check_still_loading_says_it_is_checking_and_holds_delete(provider):
    provider.eval("MR.mount(WorkspaceProviderDetail, { providerId: 'dock' }); MR.click('wp-detail-delete');")
    assert provider.eval("textOf(MR.find('wp-delete-templates-checking'))") == "Checking for templates that use this provider…"
    assert "Deletion is safe" not in provider.eval("textOf(MR.find('modal'))")
    assert js(provider, "dangerButton().props.disabled") is True


def test_a_cut_short_provider_check_keeps_its_sentence(provider):
    """The workspace provider is the one the server refuses while a template uses it, so its sentence stays."""
    provider.eval("DATA['ws:provider:dock:templates'] = { items: [{ id: 't-1', provider_id: 'other' }], truncated: true, serverTotal: 10500, error: null };")
    provider.eval("MR.mount(WorkspaceProviderDetail, { providerId: 'dock' }); MR.click('wp-detail-delete');")
    text = provider.eval("textOf(MR.find('wp-delete-templates-partial'))")
    assert text.endswith("None of these reference this provider. The rest were not checked here; the server refuses the delete if one of them does.")
