"""The canvas frame's click gate in a real browser (pivot P5, Task 26: the gate steps; audit G8-05, the lead's ruling 1 on S7 and the
review of #925). LEAD-SCHEDULED: written, collected and linted here, never run locally.

The console panel that mounts the canvas lands in S8, so these steps mount the frame the way the panel will, from a stand-in page served
on the console's own origin (``page.route``): an ``<iframe src="/canvas/frame">`` sandboxed ``allow-scripts allow-forms``, the
``primer.canvas.connect`` handshake with a ``MessageChannel``, and a ``state`` posted by the stand-in itself when the frame loads. S10
adds the panel's own steps to this file (Task 26) and may move these onto the panel.

THE RULE THESE STEPS FOLLOW: nothing Playwright runs INSIDE the canvas frame, or any frame below it, may overlap a measured window.
Playwright evaluates with a user gesture, so ``frame.evaluate``, ``locator.evaluate``, ``locator.bounding_box()`` and
``locator.wait_for()`` on a locator inside the canvas grant that frame (and its ancestors) a user activation that no pointerdown or
keydown announces: the renderer cannot claim it, and a focus-stealing element then owns it. A probe of #925 round 3 showed it: one
``bounding_box()`` inside the canvas frame with no click let a focus thief emit 3 times, one ``evaluate`` on the TOP page let it emit
nothing (an activation reaches ancestors, never the frames below). So each step:

* does every in-frame call (the mount's wait, every ``bounding_box``) during setup, then waits ``SETTLE_MS`` with no call at all, so
  the setup's activations have expired (Chromium's transient activation lasts 5 s);
* arms its attackers with a state the TOP stand-in page posts (``_arm``: an evaluate on the top page only), never with a timer that
  could fire while a setup activation is still alive;
* measures with real ``page.mouse`` / ``page.keyboard`` input at coordinates read during setup, and reads the bridge's messages, which
  the TOP stand-in keeps, with an evaluate on the top page only.
"""

from __future__ import annotations

import json
import uuid

from playwright.sync_api import Page

#: How long a step waits, calling nothing, after its last in-frame call: past Chromium's 5 s transient activation.
SETTLE_MS = 6_500

HOST = """<!doctype html><html><head><meta charset="utf-8"><title>canvas gate stand-in</title></head><body>
<iframe id="cv" src="/canvas/frame" sandbox="allow-scripts allow-forms" style="width:900px;height:700px;border:0"></iframe>
<script>
window.received = [];
var STATE = __STATE__;
var frame = document.getElementById("cv");
var port = null;
frame.addEventListener("load", function () {
  var channel = new MessageChannel();
  port = channel.port1;
  port.onmessage = function (ev) { window.received.push(ev.data); };
  frame.contentWindow.postMessage({type: "primer.canvas.connect", protocol: 1}, "*", [channel.port2]);
  port.postMessage(STATE);
});
//: Arms the attackers: the same state with every Html element's value set to "arm" (each attacker arms on its primer:value event).
window.arm = function () {
  var values = {};
  Object.keys(STATE.elements).forEach(function (id) { if (STATE.elements[id].type === "Html") values[id] = "arm"; });
  port.postMessage(Object.assign({}, STATE, {rev: STATE.rev + 1, values: values}));
};
</script></body></html>"""

#: Takes the port the bootstrap holds (agent code shares its realm and can), so a test sees what the RENDERER lets through.
CAPTURE = ("var P = null, op = MessagePort.prototype.postMessage;"
           "MessagePort.prototype.postMessage = function (m) { P = this; return op.apply(this, arguments); };")


def _on_arm(code: str) -> str:
    """Runs ``code`` once, when the canvas sets the element's value to "arm" (``_arm``)."""
    return ("addEventListener('primer:value', function (e) { if (e.detail === 'arm' && !window.armed) { window.armed = true; "
            + code + " } });")


#: Once armed, steals the focus every 50 ms and emits.
THIEF = ("<input id=bi><script>" + CAPTURE + "var n = 0;" + _on_arm(
    "setInterval(function () { if (!P) return; n++; document.getElementById('bi').focus();"
    "P.postMessage({type: 'emit', name: 'stolen', data: {n: n}}); }, 50);") + "</script>")
#: Once armed, emits whenever it has the focus.
TAB_TARGET = ("<input id=ki><script>" + CAPTURE + "var n = 0;" + _on_arm(
    "setInterval(function () { if (!P) return; if (document.hasFocus()) { n++;"
    "P.postMessage({type: 'emit', name: 'tabbed', data: {n: n}}); } }, 50);") + "</script>")
#: One click starts a loop that emits every 200 ms for as long as the page lives.
LOOPER = ("<button id=go style='width:200px;height:60px' onclick=\"var n = 0; setInterval(function () { n++;"
          "try { primer.emit('loop', {n: n}); } catch (e) {} }, 200)\">Start</button>")
#: Emits once per click.
CLICKED = "<button id=a style='width:200px;height:60px' onclick=\"primer.emit('a_click', {})\">A</button>"


def _state(elements: dict) -> dict:
    root = {"type": "Column", "props": {}, "children": list(elements)}
    return {"type": "state", "rev": 1, "elements": {"root": root, **elements}, "values": {},
            "settings": {"media_policy": "none", "media_allowlist": [], "strict_mode": False},
            "file_urls": {}, "own_origin": "", "theme": {}, "run_html": True}


def _html(body: str) -> dict:
    return {"type": "Html", "props": {"html": body, "height": 120}}


def _mount(page: Page, base_url: str, elements: dict, wait_for: str) -> None:
    """SETUP: serves the stand-in on the console's origin, opens it and waits for ``wait_for`` to be drawn (an in-frame call)."""
    url = f"{base_url}/__canvas_gate_{uuid.uuid4().hex[:8]}.html"
    body = HOST.replace("__STATE__", json.dumps(_state(elements)).replace("<", "\\u003c"))
    page.route(url, lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body))
    page.goto(url)
    page.frame_locator("#cv").locator(f"[data-cv-id={wait_for}]").wait_for(timeout=15_000)


def _box(locator) -> dict:
    """SETUP: where to click (an in-frame call, so only before ``_settle``)."""
    box = locator.bounding_box()
    assert box is not None
    return box


def _settle(page: Page) -> None:
    """Waits out every activation the setup's in-frame calls granted, calling nothing (see the module docstring)."""
    page.wait_for_timeout(SETTLE_MS)


def _arm(page: Page) -> None:
    """Arms the attackers from the TOP page (an evaluate there activates the top page only, never the canvas below it)."""
    page.evaluate("window.arm()")
    page.wait_for_timeout(300)


def _click_at(page: Page, box: dict) -> None:
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def _received(page: Page) -> list[dict]:
    """What the renderer sent the stand-in, read on the TOP page only."""
    return page.evaluate("window.received")


def _emits(page: Page) -> list[tuple[str, str]]:
    return [(m["element"], m["name"]) for m in _received(page) if m.get("type") == "event" and m.get("kind") == "emit"]


def test_a_looping_element_cannot_emit_after_its_activation_expires(page: Page, base_url: str) -> None:
    """G8-05: one click starts a loop that emits every 200 ms; at most 3 emits go out, however long it runs."""
    _mount(page, base_url, {"w": _html(LOOPER)}, "w")
    box = _box(page.frame_locator("#cv").frame_locator("[data-cv-id=w] iframe").locator("#go"))
    _settle(page)
    _click_at(page, box)
    page.wait_for_timeout(9_000)
    emits = _emits(page)
    assert 1 <= len(emits) <= 3 and all(e == ("w", "loop") for e in emits), emits


def test_a_frame_that_takes_the_focus_after_a_click_on_a_native_button_emits_nothing(page: Page, base_url: str) -> None:
    """The review of #925: a click on a native Button activates the renderer, and an Html element that was never touched and focuses
    itself gets nothing from that activation."""
    _mount(page, base_url, {"go": {"type": "Button", "props": {"label": "Go"}}, "b": _html(THIEF)}, "go")
    box = _box(page.frame_locator("#cv").locator("[data-cv-id=go]"))
    _settle(page)
    _arm(page)
    _click_at(page, box)
    page.wait_for_timeout(2_500)
    assert _emits(page) == []
    assert any(m.get("type") == "event" and m.get("element") == "go" for m in _received(page)), "the click itself went out"


def test_a_tab_from_a_native_button_into_an_html_element_lets_it_emit_nothing(page: Page, base_url: str) -> None:
    """The review of #925: a Tab press on a native Button moves the focus into an Html element; the key press is the renderer's, so the
    element gets nothing from it."""
    _mount(page, base_url, {"go2": {"type": "Button", "props": {"label": "Go2"}}, "k": _html(TAB_TARGET)}, "go2")
    box = _box(page.frame_locator("#cv").locator("[data-cv-id=go2]"))
    _click_at(page, box)            # focuses Go2; its activation expires with the setup's
    _settle(page)
    _arm(page)
    page.keyboard.press("Tab")
    page.wait_for_timeout(2_500)
    assert _emits(page) == []


def test_one_click_gives_its_activation_to_one_frame_only(page: Page, base_url: str) -> None:
    """The review of #925: the first Html element that passes the gate owns the activation, so a click spends ONE budget, in one frame.

    Known residual, accepted by the lead ("Lead rulings after the S7 review"): an element that steals the focus by script during another
    element's real click can own that activation if its message reaches the renderer first. Both frames are the same canvas's agent
    code, so it gains no gesture the agent could not already get from that click. The gate does NOT promise that the clicked element
    wins; it promises that one element gets the activation's budget and that no element double-spends it. That is what this step
    asserts.
    """
    _mount(page, base_url, {"a": _html(CLICKED), "b": _html(THIEF)}, "a")
    box = _box(page.frame_locator("#cv").frame_locator("[data-cv-id=a] iframe").locator("#a"))
    _settle(page)
    _arm(page)
    _click_at(page, box)
    page.wait_for_timeout(2_500)
    emits = _emits(page)
    owners = {element for element, _ in emits}
    assert len(owners) == 1, f"exactly one of A and B emits from the click's activation: {emits}"
    assert 1 <= len(emits) <= 3, f"the owner emits at most 3 times: {emits}"
