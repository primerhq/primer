"""The admin's canvas settings dialog (pivot P5, Task 23): ``NV_CanvasSettingsDialog`` in
``ui/components/console/nv-canvas.jsx``, run in MiniRacer on the ``mini_react_context`` harness.

The dialog is the master kit's ``PK_FormOverlay`` (title ``Canvas settings``, testid
``canvas-settings``): the media policy as a ``PK_Segmented`` (``any-https`` / ``allowlist`` /
``none``), the allowlist as a one-host-per-line textarea that shows ONLY for ``allowlist``,
a strict-mode checkbox, and the five limits (Task 5) in a ``PK_Segmented``/``PK_Section``.
Save sends the WHOLE row (every field round-trips - the master's Review Focus 1); a 422 shows
each field's error under its own control.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "components"
SHARED = UI / "shared.jsx"
FORM_FIELD = UI / "shared" / "form-field.jsx"
PLATFORM_KIT = UI / "shared" / "platform-kit.jsx"
NV_CANVAS = UI / "console" / "nv-canvas.jsx"
NV_SESSION_DOC = UI / "console" / "nv-session-doc.jsx"
SHELL_VERBS = ROOT / "ui" / "foundation" / "shell-verbs.js"

# The full CanvasSettings row (Task 5): the dialog round-trips EVERY field, so a save of
# one changed field must carry all the others unchanged.
DEFAULTS = {
    "agent_tools_enabled": True,
    "media_policy": "any-https",
    "media_allowlist": [],
    "strict_mode": False,
    "max_elements": 200,
    "max_html_bytes": 65536,
    "max_canvas_bytes": 524288,
    "max_events": 100,
    "max_waking_events_per_minute": 10,
}

PRELUDE = r"""
var window = globalThis;
window.__resources = {};
var __layers = [];
window.__con = {
  wid: "w1", doc: { kind: "session", ref: "s1" }, canvas: "open",
  setCanvas: function (s) { window.__setCanvas = s; },
  viewer: { type: "user", id: "u1", display: "Ada", role: "admin", source: "local" },
  role: "admin",
  toast: function (msg, extra) { (window.__toasts = window.__toasts || []).push([msg, extra]); },
};
window.NV_useConsole = function () { return window.__con; };
var document = { addEventListener: function () {}, removeEventListener: function () {}, body: { style: {} } };
window.primerApi = {
  useEscape: function (h, a) { __layers.push({ handler: h, active: a === undefined ? true : !!a }); },
  useFocusTrap: function () {},
  useResource: function (key, fetcher, opts) {
    return window.__resources[key] || { data: undefined, error: null, loading: true, degraded: false, refetch: function () {} };
  },
  useViewport: function () { return { isMobile: window.__mobile || false, width: 1200 }; },
  toastPush: function (t) { (window.__pushed = window.__pushed || []).push(t); },
  readRefusal: function (err, fallback) { return fallback || "an error"; },
};
window.SH_api = {
  keys: { canvas: function (sid) { return "nv-canvas:" + sid; } },
  canvas: function (wid, sid, signal) { return Promise.resolve({}); },
  canvasClear: function (wid, sid) { return Promise.resolve({}); },
  canvasEvent: function (wid, sid, body) { return Promise.resolve({}); },
  canvasSettings: function (signal) { return Promise.resolve(window.__settings || null); },
  // a thenable (not a real Promise) so the dialog's .then(ok, err) resolves synchronously
  // in the harness: ok for the success path, err(window.__putErr) for the 422 path.
  putCanvasSettings: function (body) {
    (window.PUTS = window.PUTS || []).push(body);
    return { then: function (ok, err) {
      if (window.__putErr) { if (err) err(window.__putErr); }
      else if (ok) ok({});
      return this;
    }};
  },
};
window.confirmDialog = function (opts) { return Promise.resolve(true); };
window.__ls = {};
window.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(window.__ls, k) ? window.__ls[k] : null; },
  setItem: function (k, v) { window.__ls[k] = String(v); },
  removeItem: function (k) { delete window.__ls[k]; },
};
window.crypto = { randomUUID: function () { window.__uuids = (window.__uuids || 0) + 1; return "uuid-" + window.__uuids; } };
window.open = function (url) { (window.__opened = window.__opened || []).push(url); return {}; };
window.location = { origin: "http://console.test", href: "http://console.test/console/" };
"""


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def ctx():
    py_mini_racer = pytest.importorskip("py_mini_racer")
    # the platform kit (Modal, Icon, FormField, PK_*) comes from the real shared files,
    # transpiled the way the server bundles them; the dialog reads the real shell-verbs.
    code = "\n".join(
        transpile(p) for p in (SHARED, FORM_FIELD, PLATFORM_KIT, NV_CANVAS)
    ) + "\n" + SHELL_VERBS.read_text(encoding="utf-8")
    c = mini_react_context(code, PRELUDE)
    try:
        yield c
    finally:
        c.close()


def seed(ctx, settings):
    ctx.eval("window.__settings = " + json.dumps(settings) + ";")
    ctx.eval(
        "window.__resources['nv-canvas-settings'] = { data: "
        + json.dumps(settings)
        + ", error: null, loading: false, degraded: false, refetch: function () {} };"
    )


def mount_dialog(ctx):
    ctx.eval(
        "window.PUTS = []; window.__toasts = []; window.__closed = false; "
        "MR.mount(window.NV_CanvasSettingsDialog, { onClose: function () { window.__closed = true; } });"
    )


def test_saving_one_field_keeps_every_other_field(ctx) -> None:
    # a saved strict-mode flip must round-trip the whole row (Review Focus 1): the
    # allowlist and the bumped limit go along with it, untouched.
    settings = dict(DEFAULTS)
    settings["media_allowlist"] = ["cdn.example.com"]
    settings["max_events"] = 300
    seed(ctx, settings)
    mount_dialog(ctx)
    # the form loaded (the overlay and the media segmented are up)
    assert js(ctx, '!!MR.find("canvas-settings")')
    assert js(ctx, '!!MR.find("canvas-settings-media")')
    # toggle the one changed field, then save
    ctx.eval('MR.find("canvas-settings-strict").props.onChange({ target: { checked: true } }); MR.rerender();')
    ctx.eval('MR.click("canvas-settings-save")')
    assert js(ctx, "window.PUTS") == [
        dict(DEFAULTS, media_allowlist=["cdn.example.com"], max_events=300, strict_mode=True)
    ]
    assert js(ctx, "window.__closed")
    toasts = js(ctx, "window.__toasts || []")
    assert any(t[0] == "Canvas settings saved" for t in toasts)


def test_the_allowlist_shows_only_for_listed_hosts(ctx) -> None:
    settings = dict(DEFAULTS)
    settings["media_policy"] = "allowlist"
    settings["media_allowlist"] = ["a.example.com", "b.example.com"]
    seed(ctx, settings)
    mount_dialog(ctx)
    # the allowlist shows for listed hosts, holding the current hosts one per line
    assert js(ctx, 'MR.find("canvas-settings-allowlist").props.value') == "a.example.com\nb.example.com"
    # switching the policy away hides it
    ctx.eval('MR.click("canvas-settings-media:any-https")')
    assert js(ctx, 'MR.find("canvas-settings-allowlist")') is None


def test_a_422_shows_the_field_error_under_its_field(ctx) -> None:
    settings = dict(DEFAULTS)
    settings["max_events"] = 300
    seed(ctx, settings)
    mount_dialog(ctx)
    # the server rejects the save with a 422 naming the max_events field (the thrown
    # ApiError's fieldErrors - what the component reads, never err.envelope)
    ctx.eval(
        'window.__putErr = { status: 422, '
        'fieldErrors: [ { loc: ["body", "max_events"], msg: "Input should be at least 10" } ] };'
    )
    ctx.eval('MR.click("canvas-settings-save")')
    # the field's error is shown (its message, under the limit that owns it); the dialog stays open
    assert any("Input should be at least 10" in t for t in js(ctx, "MR.texts()"))
    assert js(ctx, '!!MR.find("canvas-settings-limit:max_events")')


def test_only_admins_see_the_menu_row() -> None:
    # the header's overflow offers the row (admins only): a source gate, like the
    # canvas show/hide rows, on con.role === "admin".
    src = NV_SESSION_DOC.read_text(encoding="utf-8")
    assert 'data-testid="nv-canvas-menu-settings"' in src
    i = src.index("nv-canvas-menu-settings")
    assert 'con.role === "admin"' in src[max(0, i - 400): i]
