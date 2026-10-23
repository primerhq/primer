"""The console's toolset form against the real REST routes (audit G4: G4-03, G4-04).

The body is built by the REAL form: ``TS_NewToolsetModal`` (``ui/components/toolsets.jsx``) with the real auth section
(``ui/components/toolsets/mcp-auth.jsx``), the real platform kit and the real ``FormField``, run in MiniRacer. Its PUT is
held, sent to the real ``PUT /v1/toolsets/{id}`` here, and the server's answer is handed back to the form: a 422 as the real
``ui/foundation/api.js`` ``ApiError`` built from the envelope the server sent. Nothing about the server's side is hand-built.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.model.provider import Toolset
from tests.api.test_require_user_admin import _login, _seed
from tests.toolset.oauth._service_helpers import target_of, token_row
from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
NO_PROBE = {"allow_unreachable": "true"}
MCP_URL = "http://127.0.0.1:9/mcp"

PRELUDE = r"""
var BODIES = []; var PENDING = []; var TOASTS = []; var AUTH_ROLE = "admin";
function Icon() { return null; }
function Btn(p) { return React.createElement("button", Object.assign({ type: "button" }, p), p.children); }
function Banner(p) { return React.createElement("div", { "data-testid": "banner", className: "banner banner-" + p.kind }, p.title, p.detail); }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.children, p.footer); }
window.primerApi = {
  apiFetch: function (method, path, body) {
    if (method === "PUT" || method === "POST") {
      BODIES.push(body);
      return new Promise(function (resolve, reject) { PENDING.push({ resolve: resolve, reject: reject }); });
    }
    return Promise.resolve({ tools: [] });
  },
  useResource: function () { return { data: { role: AUTH_ROLE }, loading: false, error: null, refetch: function () {} }; },
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
// The text of the form row (the innermost element with the "field" class) that holds the control `testid`, or null.
function fieldText(testid) {
  var best = null;
  (function rec(n, rows) {
    if (n == null || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(function (c) { rec(c, rows); }); return; }
    if (!n.__el) return;
    var cls = (n.props && typeof n.props.className === "string") ? n.props.className : "";
    var next = /(^|\s)field(\s|$)/.test(cls) ? rows.concat([n]) : rows;
    if (n.props && n.props["data-testid"] === testid && next.length) best = next[next.length - 1];
    rec(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children, next);
  })(MR.find("modal"), []);
  return best ? textOf(best) : null;
}
"""


def _code() -> str:
    api = (UI / "foundation" / "api.js").read_text(encoding="utf-8")
    # The real ApiError, from a sandboxed copy of api.js: the form's own primerApi stays the stub above.
    sandboxed = "(function () { var window = {}; " + api + "\n globalThis.RealApiError = window.primerApi.ApiError; })();"
    parts = [
        transpile(UI / "components" / "shared" / "form-field.jsx"),
        transpile(UI / "components" / "shared" / "platform-kit.jsx"),
        transpile(UI / "components" / "toolsets" / "mcp-auth.jsx"),
        transpile(UI / "components" / "toolsets.jsx"),
    ]
    return sandboxed + "\n" + "\n".join(parts)


@pytest.fixture(scope="module")
def form_code() -> str:
    return _code()


class Form:
    """One mounted edit form of a served toolset row."""

    def __init__(self, code: str, row: dict, role: str) -> None:
        self.ctx = mini_react_context(code, PRELUDE)
        self.ctx.eval("AUTH_ROLE = " + json.dumps(role))
        self.ctx.eval(
            "MR.mount(TS_NewToolsetModal, { existing: " + json.dumps(row)
            + ", onClose:function(){}, onCreate:function(){}, pushToast:function(t){ TOASTS.push(t); } });"
        )

    def js(self, expression: str):
        return json.loads(self.ctx.eval("JSON.stringify(" + expression + ")"))

    def settle(self, rounds: int = 4) -> None:
        for _ in range(rounds):
            self.ctx.eval("MR.rerender()")

    def type_into(self, testid: str, value: str) -> None:
        self.ctx.eval(f"MR.find({json.dumps(testid)}).props.onChange({{ target: {{ value: {json.dumps(value)} }} }})")
        self.settle(2)

    def click(self, testid: str) -> None:
        self.ctx.eval(f"MR.click({json.dumps(testid)})")
        self.settle(2)

    def save(self) -> dict:
        """Press Save and return the body the form sent; the request stays open until answer()."""
        sent = self.js("BODIES.length")
        self.click("toolset-create-btn")
        assert self.js("BODIES.length") == sent + 1, "the form sent nothing"
        return self.js(f"BODIES[{sent}]")

    def answer(self, response) -> None:
        """Hand the server's answer to the open request: the row, or the real ApiError of its problem envelope."""
        payload = json.dumps(response.json())
        if response.status_code < 400:
            self.ctx.eval(f"PENDING[PENDING.length - 1].resolve({payload})")
        else:
            self.ctx.eval(f"PENDING[PENDING.length - 1].reject(new RealApiError({payload}))")
        self.settle(6)

    def field_text(self, testid: str) -> str | None:
        return self.js(f"fieldText({json.dumps(testid)})")

    def banner_text(self) -> str | None:
        return self.js("MR.find('toolset-form-errors') ? textOf(MR.find('toolset-form-errors')) : null")

    def close(self) -> None:
        self.ctx.close()


@pytest.fixture
def forms(form_code):
    opened: list[Form] = []

    def open_form(row: dict, role: str = "admin") -> Form:
        form = Form(form_code, row, role)
        opened.append(form)
        return form

    yield open_form
    for form in opened:
        form.close()


def _body(oauth: dict, toolset_id: str = "kb", url: str = MCP_URL) -> dict:
    return {"id": toolset_id, "provider": "mcp",
            "config": {"transport": "http", "config": {"url": url, "headers": {"X-Trace": "t"}, "oauth": oauth}}}


async def _as(raw_client, app, role: str) -> None:
    await _seed(app, uid=f"g4-{role}", username=f"g4-{role}", role=role)
    await _login(raw_client, f"g4-{role}")


async def _create(raw_client, app, oauth: dict, *, connect: bool = True) -> None:
    await _as(raw_client, app, "admin")
    r = await raw_client.post("/v1/toolsets", params=NO_PROBE, json=_body(oauth))
    assert r.status_code == 201, r.text
    if connect:
        stored = await app.state.storage_provider.get_storage(Toolset).get("kb")
        await app.state.mcp_oauth.store.replace_tokens(token_row(app.state.mcp_oauth.store, target_of(stored)))


async def _served(raw_client) -> dict:
    r = await raw_client.get("/v1/toolsets/kb")
    assert r.status_code == 200, r.text
    return r.json()


async def _stored_oauth(app) -> dict:
    stored = await app.state.storage_provider.get_storage(Toolset).get("kb")
    return stored.config.config.oauth


PUBLIC_STATIC = {"grant": "authorization_code", "scopes": ["read"], "static_client": {"client_id": "pub-client"}}
CONFIDENTIAL_STATIC = {"grant": "authorization_code", "scopes": ["read"], "static_client": {"client_id": "c1", "client_secret": "s1"}}
CLIENT_CREDENTIALS = {"grant": "client_credentials", "scopes": ["read"], "token_endpoint": "http://127.0.0.1:9/token",
                      "static_client": {"client_id": "cc", "client_secret": "cc-secret"}}


# ---- G4-03: an empty client secret whose stored value is null goes back as null --------------------------------------------------------------------------------


async def test_an_unchanged_admin_save_of_a_public_static_client_keeps_the_sign_in(forms, raw_client, app) -> None:
    await _create(raw_client, app, PUBLIC_STATIC)
    served = await _served(raw_client)
    assert served["config"]["config"]["oauth"]["static_client"]["client_secret"] is None
    form = forms(served)

    body = form.save()
    assert body["config"]["config"]["oauth"]["static_client"] == {"client_id": "pub-client", "client_secret": None}
    r = await raw_client.put("/v1/toolsets/kb", json=body)
    assert r.status_code == 200, r.text
    assert await app.state.mcp_oauth.store.get_tokens("kb") is not None, "an unchanged save signed the toolset out"


async def test_a_users_unchanged_save_of_a_public_static_client_is_accepted(forms, raw_client, app) -> None:
    await _create(raw_client, app, PUBLIC_STATIC)
    await _as(raw_client, app, "user")
    form = forms(await _served(raw_client), role="user")

    r = await raw_client.put("/v1/toolsets/kb", json=form.save())
    assert r.status_code == 200, r.text
    assert await app.state.mcp_oauth.store.get_tokens("kb") is not None


async def test_an_unchanged_save_of_a_confidential_static_client_keeps_the_sign_in(forms, raw_client, app) -> None:
    await _create(raw_client, app, CONFIDENTIAL_STATIC)
    form = forms(await _served(raw_client))

    body = form.save()
    assert body["config"]["config"]["oauth"]["static_client"] == {"client_id": "c1", "client_secret": "**********"}
    r = await raw_client.put("/v1/toolsets/kb", json=body)
    assert r.status_code == 200, r.text
    assert await app.state.mcp_oauth.store.get_tokens("kb") is not None
    assert (await _stored_oauth(app)).static_client.client_secret.get_secret_value() == "s1"


# ---- G4-04: a browser sign-in sends no token endpoint --------------------------------------------------------------------------------------------------------------


async def test_switching_client_credentials_to_browser_sign_in_saves_without_the_token_endpoint(forms, raw_client, app) -> None:
    await _create(raw_client, app, CLIENT_CREDENTIALS, connect=False)
    form = forms(await _served(raw_client))
    form.click("ts-auth-mode:oauth_browser")
    form.type_into("ts-oauth-client-secret", "browser-secret")

    body = form.save()
    oauth = body["config"]["config"]["oauth"]
    assert oauth["grant"] == "authorization_code"
    assert oauth["token_endpoint"] is None
    r = await raw_client.put("/v1/toolsets/kb", json=body)
    assert r.status_code == 200, r.text
    stored = await _stored_oauth(app)
    assert stored.grant == "authorization_code"
    assert stored.token_endpoint is None
    assert stored.static_client.client_secret.get_secret_value() == "browser-secret"


async def test_switching_with_the_masked_secret_kept_is_refused_with_its_reason(forms, raw_client, app) -> None:
    """The stored secret is kept only for its origin, and the token endpoint going away moves it: the server asks for the secret again, and
    the form says so (the refusal is no longer the silent 'token_endpoint applies to client credentials only')."""
    await _create(raw_client, app, CLIENT_CREDENTIALS, connect=False)
    form = forms(await _served(raw_client))
    form.click("ts-auth-mode:oauth_browser")

    body = form.save()
    assert body["config"]["config"]["oauth"]["token_endpoint"] is None
    r = await raw_client.put("/v1/toolsets/kb", json=body)
    assert r.status_code == 422, r.text
    assert "token_endpoint applies to client credentials only" not in r.text
    form.answer(r)
    shown = " ".join([form.banner_text() or "", json.dumps(form.js("TOASTS"))])
    assert "re-enter the secret" in shown
