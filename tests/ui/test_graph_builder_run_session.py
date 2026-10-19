"""The graph builder's Run creates the session in the DEFAULT workspace through a route that exists.

The drawer's Run used to POST ``/v1/sessions``, which has no POST route: the only create route is
``POST /v1/workspaces/{wid}/sessions`` (Plan 01's slice S6, PR #760). Starting a graph run from the
builder answered with an error toast and never opened anything. A session lives in a workspace, so
Run now creates it in the DEFAULT workspace (the ONE row ``GET /v1/workspaces`` flags ``is_default``,
a response-only flag since PR #770) and then asks the router to open the session in the studio,
naming its workspace (``navigate("/sessions/<id>", { workspace_id })``), the way the agent Chat
button opens a session. With no row flagged on any page of the list, Run shows the error toast
"No default workspace found" and sends nothing else, and it never picks the first workspace. The
list is paged (``limit=200``, then ``offset``) until the flagged row is found or the list ends, so
a default beyond the first page is found.

``GB_Builder`` (``ui/components/graph-builder/graph-builder.jsx``) runs in V8 on the shared builder
harness (``tests/ui/_graph_builder_v8.py``), with a recording ``apiFetch`` / ``useRouter`` /
``pushToast`` so a test reads what Run sends and where it goes.
"""

from __future__ import annotations

import json

from tests.ui._mini_react import mini_react_context
from tests.ui._graph_builder_v8 import builder_code

PRELUDE = r"""
window.requestAnimationFrame = function () { return 0; }; window.cancelAnimationFrame = function () {};
window.addEventListener = function () {}; window.removeEventListener = function () {};
var TOOLS = [{ id: "ts__echo", description: "echo", input_schema: { type: "object", properties: { msg: { type: "string" } }, required: ["msg"] } }];
var __seen = [];
var __navigated = [];
var __toasted = [];
var __api = {};
window.primerApi = {
  useResource: function () { return { data: { items: TOOLS }, loading: false, error: null, refetch: function () {} }; },
  useMutation: function () { return { mutate: function () {}, loading: false }; },
  usePagedList: function () { return { items: [], loading: false }; },
  Pager: function () { return null; },
  apiFetch: function (method, path, body) {
    __seen.push([method, path, body === undefined ? null : body]);
    var stub = __api[method + " " + path];
    if (!stub) return Promise.reject({ status: 500, detail: "no stub for " + method + " " + path });
    return Promise.resolve(stub);
  },
  useRouter: function () { return { navigate: function (path, opts) { __navigated.push([path, opts || null]); }, params: {} }; },
  useEscape: function () {},
  useFocusTrap: function () {},
  focusablesOf: function () { return []; },
};
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, onClick: p.onClick }, p.children); }
function Banner(p) { return React.createElement("div", null, p.children); }
function Icon() { return null; }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.title, p.children, p.footer); }
window.Btn = Btn; window.Banner = Banner; window.Icon = Icon; window.Modal = Modal;
"""

# A draft that passes every validation tier: one start step, one finish step, one connection.
DRAFT = json.dumps(
    {
        "nodes": [{"kind": "begin", "id": "s"}, {"kind": "end", "id": "f"}],
        "edges": [{"kind": "static", "from_node": "s", "to_node": "f"}],
    }
)

DRIVER = r"""
function __stub(method, path, response) { __api[method + " " + path] = response; }
var DRAFT = __draft;
function __mount() {
  MR.mount(GB_Builder, {
    graphId: "g1", loaded: DRAFT,
    pushToast: function (t) { __toasted.push(t); },
    onSaved: function () {}, onRefresh: function () {},
  });
}
function __button(text) {
  var found = null;
  (function w(n) {
    if (found || n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(w); return; }
    if (!n.__el) return;
    if (n.type === "button" && n.props && n.props.children === text) { found = n; return; }
    if (typeof n.type === "function" && n.type !== React.Fragment) w(n.out); else w(n.children);
  })(MR.find("gb-builder"));
  if (!found) throw new Error("no button with text " + text);
  return found;
}
function __event() { return { preventDefault: function () {}, stopPropagation: function () {} }; }
// The drawer's "Dry run" Btn has no test id; the drawer renders its Run button from the builder's
// validation, so one click opens the drawer and resolves the dry run (a microtask of this eval).
function __openDryRun() { __button("Dry run").props.onClick(__event()); MR.rerender(); }
function __clickRun() { __button("Run this graph").props.onClick(__event()); MR.rerender(); }
function __setSample(v) {
  var input = null;
  (function w(n) {
    if (input || n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(w); return; }
    if (!n.__el) return;
    if (n.type === "input") { input = n; return; }
    if (typeof n.type === "function" && n.type !== React.Fragment) w(n.out); else w(n.children);
  })(MR.find("gb-dryrun"));
  if (!input) throw new Error("no sample input field");
  input.props.onChange({ target: { value: v } });
  MR.rerender();
}
function __requests() { return __seen.map(function (r) { return [r[0], r[1]]; }); }
function __bodyFor(path) {
  for (var i = __seen.length - 1; i >= 0; i--) if (__seen[i][1] === path) return __seen[i][2];
  return null;
}
""".replace("__draft", DRAFT)


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx():
    ctx = mini_react_context(builder_code(), PRELUDE)
    ctx.eval(DRIVER)
    return ctx


def _stub_dry_run(ctx) -> None:
    ctx.eval(
        "__stub('POST', '/graphs/g1/dry_run', "
        "{ok: true, nodes: [], blockers: [], local: false, "
        "shape: {longest_path: 2, parallel_groups: 0, loops: 0, human_pauses: 0}})"
    )


def _stub_workspaces(ctx, pages: list[list[dict]], total: int | None) -> None:
    for i, items in enumerate(pages):
        offset = i * 200
        path = "/workspaces?limit=200" + (f"&offset={offset}" if offset else "")
        payload = {"kind": "offset", "offset": offset, "length": len(items), "items": items}
        if total is not None:
            payload["total"] = total
        ctx.eval(f"__stub('GET', {json.dumps(path)}, {json.dumps(payload)})")


def _stub_sessions(ctx, workspace_id: str, row_id: str) -> None:
    path = "/workspaces/" + workspace_id.replace("%", "%25").replace(" ", "%20").replace("/", "%2F") + "/sessions"
    ctx.eval(f"__stub('POST', {json.dumps(path)}, {{id: {json.dumps(row_id)}, workspace_id: {json.dumps(workspace_id)}}})")


def test_run_posts_the_graph_session_to_the_default_workspace_not_the_top_level_route() -> None:
    ctx = _ctx()
    try:
        _stub_workspaces(ctx, [[{"id": "ws-a"}, {"id": "ws-def", "is_default": True}]], total=2)
        _stub_sessions(ctx, "ws-def", "sess-1")
        _stub_dry_run(ctx)
        ctx.eval("__mount(); __openDryRun()")
        ctx.eval("__clickRun()")
        requests = _js(ctx, "__requests()")
        # The dry-run POST is the drawer opening itself; everything after it is what Run sends.
        assert requests == [
            ["POST", "/graphs/g1/dry_run"],
            ["GET", "/workspaces?limit=200"],
            ["POST", "/workspaces/ws-def/sessions"],
        ]
        # The bug this ticket is about: there is no POST /v1/sessions route.
        assert [r for r in requests if r[1] == "/sessions"] == []
    finally:
        ctx.close()


def test_run_opens_the_session_in_the_studio_naming_its_workspace() -> None:
    ctx = _ctx()
    try:
        _stub_workspaces(ctx, [[{"id": "ws-def", "is_default": True}]], total=1)
        _stub_sessions(ctx, "ws-def", "sess-1")
        _stub_dry_run(ctx)
        ctx.eval("__mount(); __openDryRun()")
        ctx.eval("__clickRun()")
        assert _js(ctx, "__navigated") == [["/sessions/sess-1", {"workspace_id": "ws-def"}]]
        assert _js(ctx, "__toasted") == [{"title": "Run started", "detail": "sess-1"}]
    finally:
        ctx.close()


def test_the_post_body_carries_the_graph_binding_and_auto_start() -> None:
    ctx = _ctx()
    try:
        _stub_workspaces(ctx, [[{"id": "ws-def", "is_default": True}]], total=1)
        _stub_sessions(ctx, "ws-def", "sess-1")
        _stub_dry_run(ctx)
        ctx.eval("__mount(); __openDryRun()")
        ctx.eval("__clickRun()")
        body = _js(ctx, "__bodyFor('/workspaces/ws-def/sessions')")
        assert body == {"binding": {"kind": "graph", "graph_id": "g1"}, "auto_start": True}
        # The workspace is the path, not a body field.
        assert "workspace_id" not in body
        # No sample typed, so no graph_input.
        assert "graph_input" not in body
    finally:
        ctx.close()


def test_a_default_beyond_the_first_page_is_found_by_paging() -> None:
    ctx = _ctx()
    try:
        first = [{"id": f"ws-{i}"} for i in range(200)]
        _stub_workspaces(ctx, [first, [{"id": "ws-def", "is_default": True}]], total=201)
        _stub_sessions(ctx, "ws-def", "sess-2")
        _stub_dry_run(ctx)
        ctx.eval("__mount(); __openDryRun()")
        ctx.eval("__clickRun()")
        assert _js(ctx, "__requests()") == [
            ["POST", "/graphs/g1/dry_run"],
            ["GET", "/workspaces?limit=200"],
            ["GET", "/workspaces?limit=200&offset=200"],
            ["POST", "/workspaces/ws-def/sessions"],
        ]
        assert _js(ctx, "__navigated") == [["/sessions/sess-2", {"workspace_id": "ws-def"}]]
    finally:
        ctx.close()


def test_with_no_default_workspace_run_toasts_and_posts_nothing() -> None:
    ctx = _ctx()
    try:
        # A full first page with no total, then an empty page: the list ends without a flag.
        _stub_workspaces(ctx, [[{"id": f"ws-{i}"} for i in range(200)], []], total=None)
        _stub_sessions(ctx, "ws-a", "sess-3")  # present but must never be posted to
        _stub_dry_run(ctx)
        ctx.eval("__mount(); __openDryRun()")
        ctx.eval("__clickRun()")
        requests = _js(ctx, "__requests()")
        assert [r for r in requests if r[0] == "GET"] == [
            ["GET", "/workspaces?limit=200"],
            ["GET", "/workspaces?limit=200&offset=200"],
        ]
        assert [r for r in requests if r[0] == "POST" and "/sessions" in r[1]] == []
        assert _js(ctx, "__navigated") == []
        toasts = _js(ctx, "__toasted")
        assert toasts and toasts[0]["title"] == "No default workspace found"
        assert toasts[0]["kind"] == "error"
    finally:
        ctx.close()


def test_the_flag_must_be_the_boolean_true() -> None:
    ctx = _ctx()
    try:
        # "true" (a string) and 1 (a number) both fail the strict check: none is the default.
        _stub_workspaces(ctx, [[{"id": "ws-a", "is_default": "true"}, {"id": "ws-b", "is_default": 1}]], total=2)
        _stub_sessions(ctx, "ws-a", "sess-4")
        _stub_dry_run(ctx)
        ctx.eval("__mount(); __openDryRun()")
        ctx.eval("__clickRun()")
        assert [r for r in _js(ctx, "__requests()") if r[0] == "POST" and "/sessions" in r[1]] == []
        toasts = _js(ctx, "__toasted")
        assert toasts and toasts[0]["title"] == "No default workspace found"
        assert _js(ctx, "__navigated") == []
    finally:
        ctx.close()


def test_the_workspace_id_is_url_encoded_in_the_post_path() -> None:
    ctx = _ctx()
    try:
        _stub_workspaces(ctx, [[{"id": "ws def/x", "is_default": True}]], total=1)
        _stub_sessions(ctx, "ws def/x", "sess-5")
        _stub_dry_run(ctx)
        ctx.eval("__mount(); __openDryRun()")
        ctx.eval("__clickRun()")
        assert _js(ctx, "__requests()") == [
            ["POST", "/graphs/g1/dry_run"],
            ["GET", "/workspaces?limit=200"],
            ["POST", "/workspaces/ws%20def%2Fx/sessions"],
        ]
    finally:
        ctx.close()


def test_the_sample_input_is_sent_as_graph_input() -> None:
    ctx = _ctx()
    try:
        _stub_workspaces(ctx, [[{"id": "ws-def", "is_default": True}]], total=1)
        _stub_sessions(ctx, "ws-def", "sess-6")
        _stub_dry_run(ctx)
        ctx.eval("__mount(); __openDryRun()")
        ctx.eval('__setSample(\'{"msg": "hi"}\')')
        ctx.eval("__clickRun()")
        assert _js(ctx, "__requests()") == [
            ["POST", "/graphs/g1/dry_run"],
            ["GET", "/workspaces?limit=200"],
            ["POST", "/workspaces/ws-def/sessions"],
        ]
        assert _js(ctx, "__bodyFor('/workspaces/ws-def/sessions')") == {
            "binding": {"kind": "graph", "graph_id": "g1"},
            "auto_start": True,
            "graph_input": {"msg": "hi"},
        }
    finally:
        ctx.close()
