"""Journey: the default-workspace pick never overrides a navigation that landed first (flake 01a11d41-8bd8, root cause).

``test_unknown_workspace_journey`` failed now and then in CI ("nv-ws-gone" never appeared) with the console sitting on the DEFAULT workspace and
``#/w/primer?doc=session:sess-carried-c019`` in the address bar: the workspace of the link was gone and its ``doc=`` was kept. The cause is the shell's
default-workspace effect, ``if (!wid && wsItems.length) setWid(wsItems[0].id)``: it runs after the render in which the workspace list first arrives
and tests the ``wid`` of THAT render. A hashchange handled between that render and its effect had already queued ``setWid(<the link's workspace>)``,
and the effect, still holding the stale ``wid === null``, queued ``setWid(<first workspace>)`` after it: the link's workspace lost, its document kept,
and the URL write effect then wrote the first workspace into the address bar. The window is a few milliseconds on a developer machine and long on a
loaded CI runner; the flake rate was about one run in thirty.

This puts the navigation INSIDE that window on purpose, with the real, unmodified console. An init script wraps ``React.useEffect`` and, when the
default-workspace effect is about to run with a loaded workspace list and no workspace picked, applies a hash navigation to a workspace that does not
exist and dispatches its ``hashchange`` (exactly what the browser does for a pasted link). The page must end on that workspace's not-found card.
The wrap is applied lazily: React's UMD build assigns ``window.React`` an empty object and fills it afterwards.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_gate

MISSING = "nope-flake-01a11d41"

_LAND_IN_THE_GAP = """
(() => {
  let current, patched = false;
  window.__landed = false;
  const replace = history.replaceState;
  function patch(react) {
    if (patched || !react || typeof react.useEffect !== "function") return;
    patched = true;
    const original = react.useEffect;
    react.useEffect = function (fn, deps) {
      const src = typeof fn === "function" ? String(fn) : "";
      if (src.indexOf("wsItems[0].id") < 0 || src.indexOf("setWid") < 0) return original.call(this, fn, deps);
      return original.call(this, function () {
        // deps = [wid, wsItems.length] of the render this effect belongs to.
        if (!window.__landed && deps && deps[1] > 0 && !deps[0]) {
          window.__landed = true;
          replace.call(history, null, "", "#/w/%MISSING%?doc=session:sess-carried");
          window.dispatchEvent(new HashChangeEvent("hashchange"));
        }
        return fn.apply(this, arguments);
      }, deps);
    };
  }
  Object.defineProperty(window, "React", { configurable: true, get() { patch(current); return current; }, set(v) { current = v; } });
})();
""".replace("%MISSING%", MISSING)


@pytest.mark.ui_e2e
def test_a_link_that_lands_before_the_default_workspace_is_picked_is_not_overridden(page: Page, console_url: str) -> None:
    page.add_init_script(_LAND_IN_THE_GAP)
    page.goto("about:blank")          # the page fixture already loaded the console; the wrap needs a fresh document
    open_gate(page, console_url)

    expect(page.get_by_test_id("nv-ws-gone")).to_be_visible(timeout=20_000)
    assert page.evaluate("() => window.__landed") is True, "the navigation never landed in the window: the wrap no longer matches the effect"
    assert f"#/w/{MISSING}" in page.url, page.url
    expect(page.get_by_test_id("nv-ws-gone")).to_contain_text(f"'{MISSING}' was not found")
