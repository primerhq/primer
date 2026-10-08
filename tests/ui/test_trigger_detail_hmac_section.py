"""The real trigger detail page, after a refused Clear HMAC (review of PR 542: one mutant survived every unit test).

Clear's error was stored (``setHmacError``) but the span that shows it could be dropped without a unit test noticing; only the browser journey
saw it. This renders the REAL ``TR_TriggerDetail`` (``triggers.jsx`` transpiled the way the server bundles it) in the V8 stand-in for React with
a webhook trigger that has an HMAC secret, clicks Clear, answers the PUT with the 403 and reads what the page drew.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

TRIGGERS = Path(__file__).resolve().parents[2] / "ui" / "components" / "triggers.jsx"
REASON = "Only the trigger's owner or an admin may change a webhook trigger's secrets."
# The page reads a refusal through the one reader of the foundation (ticket 01a11cd1-7aaf): the REAL api.js is loaded first and its reader is handed to the
# stand-in primerApi below (loading it after would replace the stand-in's apiFetch).
API = (Path(__file__).resolve().parents[2] / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")

_PRELUDE = API + """
var __readRefusal = window.primerApi.readRefusal;
window.location = { origin: "http://x", search: "", hash: "", pathname: "/console/" };
var document = { title: "", addEventListener: function () {}, removeEventListener: function () {} };
var __trigger = { id: "t1", slug: "hook", name: "hook", enabled: true, config: { kind: "webhook", token: "a".repeat(32), hmac_secret: "set" }, owner: null };
var __calls = []; var __failure = { name: "ApiError", status: 403, title: "Forbidden", message: "Forbidden", detail: __REASON__ };
var __confirm = true;
globalThis.confirmDialog = function () { return Promise.resolve(__confirm); };
globalThis.Btn = function (props) { return React.createElement("button", props, props.children); };
globalThis.Icon = function () { return null; };
globalThis.Banner = function () { return null; };
globalThis.Modal = function (props) { return React.createElement("div", { "data-testid": "modal" }, props.children, props.footer); };
window.primerApi = {
  readRefusal: __readRefusal,
  apiFetch: function (method, path, body) {
    __calls.push(method + " " + path);
    if (method === "PUT" && __failure) return Promise.reject(__failure);
    return Promise.resolve({});
  },
  useRouter: function () { return { navigate: function () {} }; },
  useResource: function (key) {
    return key.indexOf("trigger-subs:") === 0
      ? { data: { items: [] }, loading: false, error: null, refetch: function () {} }
      : { data: __trigger, loading: false, error: null, refetch: function () {} };
  },
};
""".replace("__REASON__", json.dumps(REASON))


@pytest.fixture
def page():
    ctx = mini_react_context(transpile(TRIGGERS), _PRELUDE)
    ctx.eval('MR.mount(TR_TriggerDetail, { id: "t1" })')
    yield ctx
    ctx.close()


def _error(ctx) -> dict | None:
    return json.loads(ctx.eval("""JSON.stringify((function () {
      var el = MR.find("hmac-error");
      return el ? { role: el.props.role, text: __text(el) } : null;
    })())"""))


_TEXT = """
function __text(n) {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(__text).join("");
  if (n.__el) return __text(typeof n.type === "function" ? n.out : n.children);
  return "";
}
"""


def _click_clear(ctx) -> None:
    ctx.eval("MR.click('clear-hmac-btn')")
    ctx.eval("MR.rerender()")          # the click's async handler continues in a microtask; draw what it left
    ctx.eval("MR.rerender()")


def test_the_page_shows_the_hmac_secret_as_configured_with_a_clear_button(page) -> None:
    assert page.eval("!!MR.find('clear-hmac-btn')") and page.eval("!!MR.find('set-hmac-btn')")
    assert _error(page) is None, "no error before anything is refused"


def test_a_refused_clear_draws_the_servers_reason_as_an_alert_beside_the_button(page) -> None:
    page.eval(_TEXT)
    _click_clear(page)
    got = _error(page)
    assert got is not None, "the refusal was stored but nothing drew it"
    assert got["role"] == "alert" and got["text"] == REASON
    assert page.eval("__calls").count("PUT /triggers/t1") >= 1, "the write was attempted"
    assert page.eval("!!MR.find('clear-hmac-btn')"), "the secret is still configured and Clear is still offered"


def test_a_declined_confirmation_writes_nothing_and_shows_nothing(page) -> None:
    page.eval(_TEXT)
    page.eval("__confirm = false")
    _click_clear(page)
    assert _error(page) is None and page.eval("__calls.length") == 0


def test_a_successful_clear_leaves_no_error(page) -> None:
    page.eval(_TEXT)
    page.eval("__failure = null")
    _click_clear(page)
    assert _error(page) is None


def test_a_new_attempt_replaces_the_old_refusal_while_it_runs(page) -> None:
    page.eval(_TEXT)
    _click_clear(page)
    assert _error(page) is not None
    page.eval("__failure = null")
    _click_clear(page)
    assert _error(page) is None, "the second attempt succeeded, so the first refusal must be gone"


def test_a_successful_set_clears_a_stale_refusal(page) -> None:
    """Clear was refused, then the user opened the Set dialog and saved a secret: the refusal about the earlier attempt must not linger."""
    page.eval(_TEXT)
    _click_clear(page)
    assert _error(page) is not None
    # Stand in for the dialog and read the props the page hands it; calling onSaved is what a successful save does.
    page.eval("TR_HmacSecretDialog = function (props) { globalThis.__dialogProps = props; return null; };")
    page.eval("MR.click('set-hmac-btn')")
    page.eval("MR.rerender()")
    assert page.eval("typeof globalThis.__dialogProps") == "object", "the Set dialog was not opened"
    page.eval("globalThis.__dialogProps.onSaved(); MR.rerender();")
    assert _error(page) is None, "a refusal from before the successful save is still on the page"
