"""Knowledge grep shows the refusal when a pattern fails (UI-L6).

``KN_GrepBox`` (``ui/components/knowledge.jsx``) ran its grep inside a
``try/finally`` with no ``catch``: an invalid regex answers 400 with the
cause, but nothing was shown and the previous hits stayed on screen as if
they were the answer. Now a refusal clears the hits and renders
``readRefusal(err).message`` under the inputs.

MiniRacer on the real file with a stubbed ``window.primerApi``: ``apiFetch``
answers the grep (a 400 refusal for a pattern containing ``bad``), and
``readRefusal`` is the one seam the console loads from ``foundation/api.js``.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

KNOWLEDGE = Path(__file__).resolve().parents[2] / "ui" / "components" / "knowledge.jsx"

REFUSAL_MESSAGE = "Invalid regular expression: missing )"

PRELUDE = r"""
globalThis.Btn = function (props) {
  return React.createElement("button", {
    "data-testid": props["data-testid"], onClick: props.onClick,
    disabled: props.disabled || !!props.busy
  }, props.children);
};
var REFUSAL = { message: "Invalid regular expression: missing )" };
var GREPS = [];
window.primerApi = {
  readRefusal: function (err) { return REFUSAL; },
  apiFetch: function (method, url) {
    GREPS.push(url);
    if (url.indexOf("q=bad") !== -1) {
      return Promise.reject({ status: 400, detail: "Invalid regular expression" });
    }
    return Promise.resolve({
      hits: [{ path: "docs/a", line: 3, excerpt: "match" }], truncated: false
    });
  }
};
"""

DRIVER = r"""
function __setQ(q) { MR.find("kn-grep-input").props.onChange({ target: { value: q } }); MR.rerender(); }
function __run() { MR.click("kn-grep-run"); }
function __hits() { return MR.findAll("kn-grep-hit").length; }
function __error() {
  var el = MR.find("kn-grep-error");
  return el && el.children && el.children[0];
}
"""


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx():
    ctx = mini_react_context(transpile(KNOWLEDGE), PRELUDE)
    ctx.eval(DRIVER)
    ctx.eval('MR.mount(KN_GrepBox, { collection: { id: "c1" }, onJump: function () {} });')
    return ctx


def test_a_good_grep_shows_the_hits() -> None:
    # Characterization: a valid pattern renders its hits.
    ctx = _ctx()
    try:
        ctx.eval('__setQ("good"); __run();')
        ctx.eval("MR.rerender();")
        assert _js(ctx, "__hits()") == 1
        assert _js(ctx, "__error()") is None
    finally:
        ctx.close()


def test_a_refusal_clears_the_hits_and_shows_the_cause() -> None:
    # An invalid regex (400) must not leave the previous hits on screen as
    # if they were the answer, and must say why.
    ctx = _ctx()
    try:
        ctx.eval('__setQ("good"); __run();')
        ctx.eval("MR.rerender();")
        assert _js(ctx, "__hits()") == 1
        ctx.eval('__setQ("bad("); __run();')
        ctx.eval("MR.rerender();")
        assert _js(ctx, "__hits()") == 0, (
            "the previous hits stayed up after a refused grep"
        )
        assert _js(ctx, "__error()") == REFUSAL_MESSAGE, (
            "the refusal carried no visible cause"
        )
    finally:
        ctx.close()


def test_a_good_grep_after_a_refusal_clears_the_error() -> None:
    # Characterization: the error state is the refusal's, not permanent.
    ctx = _ctx()
    try:
        ctx.eval('__setQ("bad("); __run();')
        ctx.eval("MR.rerender();")
        assert _js(ctx, "__error()") is not None
        ctx.eval('__setQ("good"); __run();')
        ctx.eval("MR.rerender();")
        assert _js(ctx, "__error()") is None
        assert _js(ctx, "__hits()") == 1
    finally:
        ctx.close()
