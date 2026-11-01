"""T130 (GU-4, GU-5, GU-13): the admin Edit-user dialog sends only what the admin changed, Force rotation asks first, and the Share dialog keeps its draft
when the page re-reads the view on a tab wake. Each runs the real component in MiniRacer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from py_mini_racer import MiniRacer

ROOT = Path(__file__).resolve().parents[2]
_OPEN: list = []


@pytest.fixture(autouse=True)
def _close():
    yield
    while _OPEN:
        _OPEN.pop().close()


ADMIN_PRELUDE = r"""
var __patches = [];
var __confirms = [];
var __confirmAnswer = true;
window.primerApi = {
  apiFetch: function (method, path, body) { __patches.push([method, path, body]); return Promise.resolve({ generated_password: "tmp-pass-1" }); },
  readRefusal: function (e) { return { code: null, message: String(e) }; },
};
var Modal = function (p) { return React.createElement("div", { "data-testid": "modal" }, p.children, p.footer); };
var Btn = function (p) { return React.createElement("button", { "data-testid": p["data-testid"], onClick: p.onClick, disabled: p.disabled }, p.children); };
var Banner = function () { return null; };
var Icon = function () { return null; };
var confirmDialog = function (prompt) { __confirms.push(prompt); return Promise.resolve(__confirmAnswer); };
"""


def _admin_ctx():
    from ui._mini_react import mini_react_context, transpile

    ctx = mini_react_context(transpile(ROOT / "ui" / "components" / "admin_users.jsx"), prelude=ADMIN_PRELUDE)
    _OPEN.append(ctx)
    return ctx


def _mount_edit(ctx, user: dict) -> None:
    ctx.eval("MR.mount(ADM_EditUserDialog, { user: " + json.dumps(user) + ", onClose: function () {}, onSaved: function () {} })")


def _type_email(ctx, value: str) -> None:
    ctx.eval(
        "(function () { var el = null; (function walk(n){ if (!n || el) return; if (Array.isArray(n)) { n.forEach(walk); return; }"
        " if (n.__el) { if (n.props && n.props.id === 'adm-edit-email') { el = n; return; }"
        " walk(typeof n.type === 'function' ? n.out : n.children); } })(MR.find('adm-edit-form'));"
        " el.props.onChange({ target: { value: " + json.dumps(value) + " } }); MR.rerender(); })()"
    )


def _patches(ctx) -> list:
    return json.loads(ctx.eval("JSON.stringify(__patches)"))


BOB = {"id": "u-bob", "username": "bob", "email": "old@x.io", "role": "user", "disabled": False}


def test_changing_only_the_email_sends_only_the_email():
    ctx = _admin_ctx()
    _mount_edit(ctx, BOB)
    _type_email(ctx, "new@x.io")
    ctx.eval('MR.click("edit-user-submit")')
    (method, path, body), = _patches(ctx)
    assert (method, path) == ("PATCH", "/admin/users/u-bob")
    assert body == {"email": "new@x.io"}


def test_clearing_the_email_sends_null_and_a_typed_password_rides_along():
    ctx = _admin_ctx()
    _mount_edit(ctx, BOB)
    _type_email(ctx, "")
    ctx.eval(
        "(function () { var el = null; (function walk(n){ if (!n || el) return; if (Array.isArray(n)) { n.forEach(walk); return; }"
        " if (n.__el) { if (n.props && n.props.type === 'password') { el = n; return; } walk(typeof n.type === 'function' ? n.out : n.children); } })(MR.find('adm-edit-form'));"
        " el.props.onChange({ target: { value: 'a-long-password' } }); MR.rerender(); })()"
    )
    ctx.eval('MR.click("edit-user-submit")')
    (_m, _p, body), = _patches(ctx)
    assert body == {"email": None, "password": "a-long-password"}


def test_a_dialog_nothing_was_changed_in_sends_nothing():
    ctx = _admin_ctx()
    _mount_edit(ctx, BOB)
    ctx.eval('MR.click("edit-user-submit")')
    assert _patches(ctx) == []


def _flush(ctx) -> None:
    ctx.eval("1")        # promises of the stubs resolve between evals


def test_force_rotation_asks_first_and_sends_nothing_when_declined():
    ctx = _admin_ctx()
    ctx.eval("__confirmAnswer = false;")
    ctx.eval('MR.mount(ADM_UserRow, { user: ' + json.dumps(BOB) + ', onEdit: function () {}, onDelete: function () {}, onKeys: function () {}, onChanged: function () {} })')
    ctx.eval('MR.click("force-rotation-btn-u-bob")')
    _flush(ctx)
    confirms = json.loads(ctx.eval("JSON.stringify(__confirms)"))
    assert len(confirms) == 1 and confirms[0]["danger"] is True and "bob" in confirms[0]["title"]
    assert _patches(ctx) == []


def test_force_rotation_sends_the_rotation_once_confirmed():
    ctx = _admin_ctx()
    ctx.eval('MR.mount(ADM_UserRow, { user: ' + json.dumps(BOB) + ', onEdit: function () {}, onDelete: function () {}, onKeys: function () {}, onChanged: function () {} })')
    ctx.eval('MR.click("force-rotation-btn-u-bob")')
    _flush(ctx)
    assert [p[2] for p in _patches(ctx)] == [{"generate_password": True}]


VIEW = {"owners": [{"id": "u-1", "username": "amy"}], "grants": [], "public": False, "public_tools": [], "rev": 7, "your_level": "owner"}


def test_the_share_dialogs_access_read_does_not_refetch_on_wake():
    from ui._mini_react import mini_react_context, transpile
    from ui.test_share_dialog import DIALOG_PRELUDE

    prelude = DIALOG_PRELUDE + (
        "var __resKey = null; var __mutFetch = null; var __resOpts = null;"
        "var __origUR = window.primerApi.useResource;"
        "window.primerApi.useResource = function (k, f, o) { __resOpts = o; return __origUR(k, f, o); };"
        "__resState = { data: " + json.dumps(VIEW) + ", error: null, loading: false };"
    )
    ctx = mini_react_context(transpile(ROOT / "ui" / "components" / "shared" / "share-dialog.jsx"), prelude=prelude)
    _OPEN.append(ctx)
    ctx.eval('MR.mount(SHR_ShareDialog, { plural: "agents", id: "a-1", title: "A", onClose: function () {} })')
    assert json.loads(ctx.eval("JSON.stringify(__resOpts || {})")).get("refetchOnWake") is False


def test_a_numeric_enum_select_hands_back_the_number_and_blank_clears_to_null() -> None:
    """GU-19: the harness overrides select returned "3" for an enum of numbers and "" for a cleared one."""
    import re

    src = (ROOT / "ui" / "components" / "harness_form.jsx").read_text(encoding="utf-8")
    fn = re.search(r"function HF_enumPick\(.*?\n}\n", src, re.S).group(0)
    ctx = MiniRacer()
    ctx.eval(fn)
    assert ctx.eval("HF_enumPick([1, 3, 5], '3')") == 3
    assert ctx.eval("typeof HF_enumPick([1, 3, 5], '3')") == "number"
    assert ctx.eval("HF_enumPick(['a', 'b'], 'b')") == "b"
    assert ctx.eval("HF_enumPick([1, 3], '')") is None
    assert "onChange(HF_enumPick(schema.enum, e.target.value))" in src


def test_the_edit_forms_have_an_explicit_clear_for_an_optional_secret() -> None:
    """GU-20 (source checks; these dialogs have no render harness here): blank keeps, only the Clear control removes."""
    sso = (ROOT / "ui" / "components" / "sso_admin.jsx").read_text(encoding="utf-8")
    assert "?clear_client_secret=true" in sso and 'data-testid="sso-clear-secret"' in sso
    assert "...(clientSecret && !clearSecret ? { client_secret: clientSecret } : {})" in sso, "blank still omits the secret (keep)"
    channels = (ROOT / "ui" / "components" / "channels.jsx").read_text(encoding="utf-8")
    assert "if (isEdit && f.secret && cleared[f.key]) {\n        out[f.key] = null;" in channels
    assert 'data-testid={"ch-clear-" + f.key}' in channels
    assert "!f.required && existing?.config?.[f.key]" in channels, "a required secret cannot be cleared"
