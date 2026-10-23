"""The toolset form reads a 422's locs the way the server writes them (audit G4-05).

pydantic names a field inside a union through the member it tried, and the toolset PUT route validates the body itself, so the locs carry
validator and member segments and no leading ``body``. The form keyed its errors by the raw loc, so no field ever matched and a refused
save left the dialog open with nothing on screen. The error lists below are what ``PUT /v1/toolsets/kb`` answered (loc and msg verbatim);
tests/api/test_toolset_console_oauth_form.py runs the same form against the live route.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
WRAPPER = "function-after[_validate_config_matches_transport(), McpConfig]"
PYTHON_NOISE = [
    {"type": "missing", "loc": ["config", "PythonConfig", "source"], "msg": "Field required"},
    {"type": "missing", "loc": ["config", "PythonConfig", "source_version"], "msg": "Field required"},
    {"type": "extra_forbidden", "loc": ["config", "PythonConfig", "transport"], "msg": "Extra inputs are not permitted"},
    {"type": "extra_forbidden", "loc": ["config", "PythonConfig", "config"], "msg": "Extra inputs are not permitted"},
]
STDIO_NOISE = {"type": "missing", "loc": ["config", WRAPPER, "config", "StdioConfig", "command"], "msg": "Field required"}
TOKEN_ENDPOINT = {"type": "value_error", "loc": ["config", WRAPPER, "config", "HttpConfig", "oauth", "token_endpoint"],
                  "msg": "Value error, token_endpoint must be an http or https URL with a host"}
OAUTH_BLOCK = {"type": "value_error", "loc": ["config", WRAPPER, "config", "HttpConfig", "oauth"],
               "msg": "Value error, token_endpoint applies to client credentials only"}
RESOURCE = {"type": "value_error", "loc": ["config", WRAPPER, "config", "HttpConfig", "oauth", "resource_uri"], "msg": "Value error, resource_uri is refused"}

PRELUDE = r"""
var BODIES = []; var TOASTS = [];
function Icon() { return null; }
function Btn(p) { return React.createElement("button", Object.assign({ type: "button" }, p), p.children); }
function Banner(p) { return React.createElement("div", { "data-testid": "banner" }, p.title, p.detail); }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.children, p.footer); }
window.primerApi = {
  apiFetch: function (method, path, body) {
    if (method === "PUT" || method === "POST") { BODIES.push(body); return Promise.reject(window.__ERR); }
    return Promise.resolve({});
  },
  useResource: function () { return { data: { role: "admin" }, loading: false, error: null, refetch: function () {} }; },
  useViewport: function () { return { isMobile: false }; },
  useMutation: function (fetcher, opts) { return { loading: false, mutate: function (arg) {
    return Promise.resolve(fetcher(arg)).then(function (d) { if (opts.onSuccess) opts.onSuccess(d); return d; },
      function (e) { if (opts.onError) opts.onError(e); throw e; });
  } }; },
};
function textOf(node) {
  var out = [];
  (function collect(n) {
    if (n == null || typeof n === "boolean") return;
    if (typeof n === "string" || typeof n === "number") { out.push(String(n)); return; }
    if (Array.isArray(n)) { n.forEach(collect); return; }
    if (n.__el) collect(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  })(node);
  return out.join(" ");
}
"""


def _code() -> str:
    return "\n".join(
        transpile(UI / "components" / path)
        for path in ("shared/form-field.jsx", "shared/platform-kit.jsx", "toolsets/mcp-auth.jsx", "toolsets.jsx")
    )


@pytest.fixture(scope="module")
def code() -> str:
    return _code()


@pytest.fixture
def ctx(code):
    context = mini_react_context(code, PRELUDE)
    try:
        yield context
    finally:
        context.close()


def js(ctx, expression: str):
    return json.loads(ctx.eval("JSON.stringify(" + expression + ")"))


@pytest.mark.parametrize(
    ("loc", "key", "members"),
    [
        (TOKEN_ENDPOINT["loc"], "body.config.config.oauth.token_endpoint", ["McpConfig", "HttpConfig"]),
        (STDIO_NOISE["loc"], "body.config.config.command", ["McpConfig", "StdioConfig"]),
        (["config", "PythonConfig", "source"], "body.config.source", ["PythonConfig"]),
        (["body", "config", WRAPPER, "config", "HttpConfig", "url"], "body.config.config.url", ["McpConfig", "HttpConfig"]),
        (["id"], "body.id", []),
        (["body", "id"], "body.id", []),
        (["query", "allow_unreachable"], "query.allow_unreachable", []),
        (["config", WRAPPER, "config", "HttpConfig", "headers", "Authorization"], "body.config.config.headers.Authorization", ["McpConfig", "HttpConfig"]),
        (["config", "tagged-union[A,B]", "x"], "body.config.x", []),
        ([], "body", []),
    ],
)
def test_a_loc_becomes_the_field_path(ctx, loc: list, key: str, members: list) -> None:
    assert js(ctx, f"TS_errorPath({json.dumps(loc)})") == {"key": key, "members": members}


def test_only_the_chosen_members_errors_are_kept(ctx) -> None:
    errors = [STDIO_NOISE, TOKEN_ENDPOINT, *PYTHON_NOISE]
    http = js(ctx, f"TS_fieldErrorsOf({json.dumps(errors)}, TS_bodyMembers('mcp', 'http'))")
    assert http == {"body.config.config.oauth.token_endpoint": "token_endpoint must be an http or https URL with a host"}
    sse = js(ctx, f"TS_fieldErrorsOf({json.dumps(errors)}, TS_bodyMembers('mcp', 'sse'))")
    assert sse == http
    stdio = js(ctx, f"TS_fieldErrorsOf({json.dumps(errors)}, TS_bodyMembers('mcp', 'stdio'))")
    assert stdio == {"body.config.config.command": "Field required"}


def test_errors_of_members_the_form_does_not_know_are_all_kept(ctx) -> None:
    errors = [{"type": "missing", "loc": ["config", "WebSocketConfig", "url"], "msg": "Field required"}]
    got = js(ctx, f"TS_fieldErrorsOf({json.dumps(errors)}, TS_bodyMembers('mcp', 'http'))")
    assert got == {"body.config.WebSocketConfig.url": "Field required"}


@pytest.mark.parametrize(
    ("mode", "keys"),
    [
        ("none", []),
        ("header", []),
        ("oauth_browser", ["oauth.static_client.client_id", "oauth.static_client.client_secret", "oauth.scopes", "oauth.resource_uri"]),
        ("oauth_client_credentials", ["oauth.static_client.client_id", "oauth.static_client.client_secret", "oauth.scopes", "oauth.token_endpoint"]),
    ],
)
def test_the_auth_section_names_the_keys_it_draws(ctx, mode: str, keys: list) -> None:
    assert js(ctx, f"TS_authErrorKeys({{mode: {json.dumps(mode)}}})") == keys


def _mount(ctx, oauth: dict | None) -> None:
    config = {"url": "https://m.example/mcp", "headers": {}}
    if oauth is not None:
        config["oauth"] = oauth
    row = {"id": "kb", "provider": "mcp", "config": {"transport": "http", "config": config}}
    ctx.eval("MR.mount(TS_NewToolsetModal, { existing: " + json.dumps(row)
             + ", onClose:function(){}, onCreate:function(){}, pushToast:function(t){ TOASTS.push(t); } });")


def _refuse(ctx, errors: list) -> None:
    err = {"status": 422, "type": "/errors/validation-error", "title": "Data is incomplete", "detail": "Missing or invalid: ...", "fieldErrors": errors}
    ctx.eval("window.__ERR = " + json.dumps(err))
    ctx.eval("MR.click('toolset-create-btn')")
    for _ in range(6):
        ctx.eval("MR.rerender()")


def test_an_error_no_field_draws_is_in_the_banner_with_its_path(ctx) -> None:
    _mount(ctx, {"grant": "authorization_code", "scopes": []})
    _refuse(ctx, [STDIO_NOISE, OAUTH_BLOCK, *PYTHON_NOISE])
    banner = js(ctx, "textOf(MR.find('toolset-form-errors'))")
    assert "Could not save the toolset" in banner
    assert "oauth" in banner and "token_endpoint applies to client credentials only" in banner
    assert "Field required" not in banner and "Extra inputs" not in banner
    assert ctx.eval("MR.find('toolset-form-errors').props.role") == "alert"
    assert js(ctx, "TOASTS") == []


def test_a_field_hidden_by_the_mode_goes_to_the_banner(ctx) -> None:
    """A token endpoint error while the form is in browser mode has no field on screen to sit under."""
    _mount(ctx, {"grant": "authorization_code", "scopes": []})
    _refuse(ctx, [TOKEN_ENDPOINT])
    assert "oauth.token_endpoint" in js(ctx, "textOf(MR.find('toolset-form-errors'))")


def test_a_refused_resource_is_drawn_outside_the_fold(ctx) -> None:
    _mount(ctx, {"grant": "authorization_code", "scopes": []})
    assert ctx.eval("MR.find('ts-oauth-resource') === null"), "the resource field starts folded"
    _refuse(ctx, [STDIO_NOISE, RESOURCE])
    assert ctx.eval("MR.find('ts-oauth-resource') !== null")
    assert "resource_uri is refused" in " ".join(js(ctx, "MR.texts()"))
    assert ctx.eval("MR.find('toolset-form-errors') === null")


def test_no_banner_without_an_error(ctx) -> None:
    _mount(ctx, {"grant": "authorization_code", "scopes": []})
    assert ctx.eval("MR.find('toolset-form-errors') === null")
