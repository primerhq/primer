"""Journey: a hash change that lands while the shell is mounting is not lost (found in CI, 2026-10-08).

The shell derives its state from the URL hash at first render, registers its ``hashchange`` listener in an effect, and in a LATER effect
writes the URL back from its state when the two differ. A hash that changed between the first render and those effects (a link
followed while the console boots, or a test that sets ``location.hash`` right after ``goto``) was therefore overwritten by the write
effect with the URL of the stale first-render state, before any event could be read: the navigation vanished and the console stayed on
the studio. ``open_legacy_route`` / ``open_view`` and about one ui-e2e run in thirty hit it (``channels/rules`` in CI, ``providers/*``
locally), always as a 45 s wait for an overlay that never opens.

Both shapes of "the hash changed in that window" are made deterministic by an init script that moves the hash the moment the shell
registers its ``hashchange`` listener: silently (``replaceState``: no event ever fires, so the shell must notice when it writes) and with
an event (``location.hash =``: the event is queued behind the effects, and the write effect used to clobber the hash first).
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import shell_url

_MOVE_THE_HASH = """
(() => {
  const target = %(target)s;
  const real = EventTarget.prototype.addEventListener;
  let moved = false;
  EventTarget.prototype.addEventListener = function (type, listener, opts) {
    if (this === window && type === 'hashchange' && !moved) {
      moved = true;
      %(move)s
    }
    return real.call(this, type, listener, opts);
  };
})();
"""


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        return c.get("/v1/workspaces").json()["items"][0]["id"]


def _script(target: str, move: str) -> str:
    import json

    return _MOVE_THE_HASH % {"target": json.dumps(target), "move": move}


@pytest.mark.ui_e2e
@pytest.mark.parametrize("how", ["silently", "with an event"])
def test_a_hash_change_during_the_mount_is_read_not_overwritten(page: Page, base_url: str, console_url: str, how: str) -> None:
    wid = _a_workspace_id(base_url)
    target = f"#/w/{wid}?overlay=agents"
    move = "history.replaceState(null, '', target);" if how == "silently" else "window.location.hash = target;"
    page.add_init_script(_script(target, move))
    # The ``page`` fixture already loaded the console root, so going straight to the shell URL would be a same-document hash change:
    # no new document, no init script, no mount. Leave the page first.
    page.goto("about:blank")
    page.goto(shell_url(console_url, wid), wait_until="domcontentloaded")

    expect(page.get_by_test_id("nv-overlay:agents")).to_be_visible(timeout=20_000)
    assert page.evaluate("() => window.location.hash") == target, "the address bar is the one the link named"
