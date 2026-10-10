"""Journey: what the focus trap counts as a stop is what Chromium tabs to (review of #729, round 2, nit 2).

``focusablesOf`` is compared with the browser itself: a blank page serves the real ``ui/foundation/focus-trap.js`` and holds a box between two buttons; Tab is pressed from the first button until the second, and the
controls it visited are the oracle. The cases are the ones that were still counted as stops and leaked at a dialog's end: the descendants of a ``<fieldset disabled>``, an ``[inert]`` subtree, ``tabindex=""``
and a ``<summary>`` that is not the first of its ``<details>``; the plain ones must keep agreeing.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

HARNESS = """<!doctype html><html><head><meta charset="utf-8"><title>stops</title></head><body>
<button id="s1">s1</button>
<div id="D"></div>
<button id="s2">s2</button>
<script src="/console/foundation/focus-trap.js"></script>
</body></html>"""

CASES = {
    "plain": '<button id="a">a</button><input id="b"><a id="c" href="#x">c</a>',
    "fieldset-disabled": '<fieldset disabled><input id="a"><button id="b">b</button></fieldset><input id="c">',
    "inert-subtree": '<div inert><button id="a">a</button></div><button id="b">b</button>',
    "inert-element": '<button id="a" inert>a</button><button id="b">b</button>',
    "tabindex-empty": '<div id="a" tabindex="">x</div><button id="b">b</button>',
    "tabindex-zero": '<div id="a" tabindex="0">x</div><button id="b">b</button>',
    "tabindex-minus-one": '<button id="a" tabindex="-1">a</button><div id="b" tabindex="-1">b</div><button id="c">c</button>',
    "orphan-summary": '<details open><summary id="a">A</summary><summary id="b">B</summary></details><button id="c">c</button>',
    "radio-group": '<input type="radio" name="g" id="a"><input type="RADIO" name="g" id="b" checked><input type="radio" name="g" id="c"><button id="d">d</button>',
}

def _tab_order(page: Page) -> list[str]:
    page.evaluate("() => document.getElementById('s1').focus()")
    seen: list[str] = []
    for _ in range(12):
        page.keyboard.press("Tab")
        now = page.evaluate("() => document.activeElement === document.body ? 'BODY' : (document.activeElement.id || document.activeElement.tagName)")
        if now in ("s2", "BODY"):
            break
        seen.append(now)
    return seen


@pytest.mark.ui_e2e
@pytest.mark.parametrize("case", sorted(CASES))
def test_focusables_of_is_what_the_browser_tabs_to(page: Page, base_url: str, case: str) -> None:
    page.route("**/__stops_harness.html", lambda route: route.fulfill(status=200, content_type="text/html", body=HARNESS))
    page.goto(base_url + "/__stops_harness.html")
    page.wait_for_function("() => !!(window.primerApi && window.primerApi.focusablesOf)", timeout=15_000)
    page.evaluate("(html) => { document.getElementById('D').innerHTML = html; }", CASES[case])
    browser = _tab_order(page)
    hook = page.evaluate("() => window.primerApi.focusablesOf(document.getElementById('D')).map((e) => e.id)")
    assert hook == browser, f"{case}: the trap counts {hook}, Chromium tabs to {browser}"
