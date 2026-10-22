"""The MCP toolset form keeps the whole auth block while editing the visible fields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
MCP_AUTH = UI / "components" / "toolsets" / "mcp-auth.jsx"
TOOLSETS = UI / "components" / "toolsets.jsx"

PRELUDE = r"""
function PK_Section(p) { return React.createElement("section", { "data-testid": p.testid }, p.title, p.children); }
function PK_Segmented(p) { return React.createElement("div", { "data-testid": p.testid, "aria-label": p.ariaLabel },
  (p.options || []).map(function (o) { return React.createElement("button", { key: o.value, "data-testid": p.testid + ":" + o.value,
    onClick: function () { p.onChange(o.value); } }, o.label); })); }
function PK_Grid(p) { return React.createElement("div", { "data-testid": "grid", "data-cols": String(p.cols) }, p.children); }
function PK_Field(p) { return React.createElement("div", { "data-testid": p.testid || null }, p.label, p.help || null, p.error || null, p.children); }
function PK_Fold(p) { return React.createElement("div", { "data-testid": p.testid }, p.label, p.children); }
function TS_KvEditor(p) { return React.createElement("div", { "data-testid": p.testid || "kv" }, p.label); }
"""


def js(ctx, expression: str):
    return json.loads(ctx.eval("JSON.stringify(" + expression + ")"))


@pytest.fixture
def auth():
    ctx = mini_react_context(transpile(MCP_AUTH), PRELUDE)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.mark.parametrize(
    ("cfg", "mode"),
    [
        ({}, "none"),
        ({"headers": {"A": "b"}}, "header"),
        ({"oauth": {}}, "oauth_browser"),
        ({"oauth": {"grant": "authorization_code"}}, "oauth_browser"),
        ({"oauth": {"grant": "client_credentials"}}, "oauth_client_credentials"),
    ],
)
def test_mode_of_a_stored_config(auth, cfg: dict, mode: str) -> None:
    assert auth.eval(f"TS_authModeOf({json.dumps(cfg)})") == mode


def test_apply_keeps_every_field_the_section_does_not_show(auth) -> None:
    stored = {
        "url": "https://m.example/mcp",
        "headers": {"X-Tenant": "**********"},
        "future": 1,
        "oauth": {
            "grant": "authorization_code",
            "scopes": ["a"],
            "resource_uri": "https://r.example",
            "static_client": {"client_id": "c", "client_secret": "**********"},
            "client_name": "primer-x",
            "future_key": 1,
        },
    }
    got = js(
        auth,
        f"TS_applyAuth({json.dumps(stored)}, Object.assign(TS_authFormOf({json.dumps(stored)}), {{ scopes: 'a b' }}))",
    )
    expected = json.loads(json.dumps(stored))
    expected["oauth"]["scopes"] = ["a", "b"]
    assert got == expected


def test_empty_optional_fields_are_null_not_empty_strings(auth) -> None:
    form = {
        "mode": "oauth_browser",
        "headers": [],
        "scopes": "",
        "clientId": "",
        "clientSecret": "",
        "resource": "",
        "tokenEndpoint": "",
    }
    got = js(auth, f"TS_applyAuth({{}}, {json.dumps(form)})")
    assert got["oauth"] == {
        "grant": "authorization_code",
        "scopes": [],
        "static_client": None,
        "resource_uri": None,
    }


def test_switching_to_none_clears_oauth_and_headers(auth) -> None:
    got = js(auth, "TS_applyAuth({oauth:{grant:'authorization_code'},headers:{A:'b'},future:1}, {mode:'none',headers:[]})")
    assert got == {"oauth": None, "headers": {}, "future": 1}


def test_switching_to_header_drops_the_oauth_block(auth) -> None:
    form = {"mode": "header", "headers": [{"key": "Authorization", "value": "Bearer x"}]}
    got = js(auth, f"TS_applyAuth({{oauth:{{grant:'authorization_code'}},future:1}}, {json.dumps(form)})")
    assert got == {"oauth": None, "headers": {"Authorization": "Bearer x"}, "future": 1}


def test_client_credentials_needs_id_and_secret(auth) -> None:
    assert js(
        auth,
        "TS_validateAuth({mode:'oauth_client_credentials',clientId:'',clientSecret:''})",
    ) == {
        "oauth.static_client.client_id": "Required",
        "oauth.static_client.client_secret": "Required",
    }


def test_the_section_offers_four_modes_with_their_labels(auth) -> None:
    assert js(auth, "TS_AUTH_MODES") == [
        {"value": "none", "label": "None"},
        {"value": "header", "label": "Header"},
        {"value": "oauth_browser", "label": "OAuth (browser sign-in)"},
        {"value": "oauth_client_credentials", "label": "OAuth (client credentials)"},
    ]


@pytest.mark.parametrize(
    ("mode", "present", "absent"),
    [
        ("header", ["ts-auth-headers"], ["ts-oauth-client-id"]),
        ("oauth_browser", ["ts-oauth-client-id", "ts-oauth-client-secret", "ts-oauth-scopes", "ts-oauth-resource"], ["ts-oauth-token-endpoint"]),
        ("oauth_client_credentials", ["ts-oauth-client-id", "ts-oauth-client-secret", "ts-oauth-scopes", "ts-oauth-token-endpoint"], ["ts-oauth-resource"]),
    ],
)
def test_each_mode_shows_its_fields(auth, mode: str, present: list[str], absent: list[str]) -> None:
    form = {
        "mode": mode,
        "headers": [],
        "scopes": "",
        "clientId": "",
        "clientSecret": "",
        "resource": "",
        "tokenEndpoint": "",
    }
    auth.eval(
        "MR.mount(TS_McpAuthSection, { form: "
        + json.dumps(form)
        + ", onChange: function(){}, errors: {}, locked: false, isMobile: false });"
    )
    assert auth.eval('MR.find("ts-auth-section") !== null')
    assert auth.eval('MR.find("grid").props["data-cols"]') == "2"
    for testid in present:
        assert auth.eval(f'MR.find({json.dumps(testid)}) !== null'), testid
    for testid in absent:
        assert not auth.eval(f'MR.find({json.dumps(testid)}) !== null'), testid


FORM_PRELUDE = PRELUDE + r"""
var AUTH_ROLE = "admin"; var BODIES = [];
function Btn(p) { return React.createElement("button", Object.assign({ type: "button" }, p), p.children); }
function Icon() { return null; }
function Banner() { return null; }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.children, p.footer); }
function FormField(p) { return React.createElement("div", null, p.children); }
window.primerApi = {
  apiFetch: function (method, path, body) { if (method === "PUT") BODIES.push(body); return Promise.resolve(body || {}); },
  useResource: function () { return { data: { role: AUTH_ROLE }, loading: false, error: null, refetch: function () {} }; },
  useViewport: function () { return { isMobile: false }; },
  useMutation: function (fetcher, opts) { return { loading: false, mutate: function (arg) {
    return Promise.resolve(fetcher(arg)).then(function (d) { if (opts.onSuccess) opts.onSuccess(d); return d; }, function (e) { if (opts.onError) opts.onError(e); throw e; });
  } }; },
};
"""


def _mount_edit(role: str = "admin"):
    ctx = mini_react_context(transpile(MCP_AUTH) + "\n" + transpile(TOOLSETS), FORM_PRELUDE)
    row = {
        "id": "kb",
        "provider": "mcp",
        "config": {
            "transport": "http",
            "config": {
                "url": "https://m.example/mcp",
                "headers": {"X-Tenant": "**********"},
                "oauth": {
                    "grant": "authorization_code",
                    "scopes": ["a"],
                    "resource_uri": "https://r.example",
                    "static_client": {"client_id": "c", "client_secret": "**********"},
                    "client_name": "primer-x",
                    "future_key": 1,
                },
            },
        },
    }
    ctx.eval("AUTH_ROLE = " + json.dumps(role))
    ctx.eval(
        "MR.mount(TS_NewToolsetModal, { existing: "
        + json.dumps(row)
        + ", onClose:function(){}, onCreate:function(){}, pushToast:function(){} });"
    )
    return ctx


def _settle(ctx, rounds: int = 6) -> None:
    for _ in range(rounds):
        ctx.eval("MR.rerender()")


def test_an_edit_saves_the_whole_auth_block() -> None:
    ctx = _mount_edit()
    try:
        ctx.eval("MR.find('ts-oauth-scopes').props.onChange({target:{value:'a b'}})")
        _settle(ctx, 2)
        ctx.eval("MR.click('toolset-create-btn')")
        _settle(ctx)
        oauth = js(ctx, "BODIES[0].config.config.oauth")
        assert oauth == {
            "grant": "authorization_code",
            "scopes": ["a", "b"],
            "resource_uri": "https://r.example",
            "static_client": {"client_id": "c", "client_secret": "**********"},
            "client_name": "primer-x",
            "future_key": 1,
        }
    finally:
        ctx.close()


def test_a_non_admin_sees_an_oauth_toolset_locked() -> None:
    ctx = _mount_edit("user")
    try:
        assert ctx.eval("!!MR.find('toolset-url-input').props.disabled")
        assert ctx.eval("MR.find('ts-auth-locked') !== null")
        assert ctx.eval("!!MR.find('ts-oauth-scopes').props.disabled")
    finally:
        ctx.close()


def test_the_bundle_transpiles_with_the_auth_section() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    assert "TS_McpAuthSection" in body.decode("utf-8")
