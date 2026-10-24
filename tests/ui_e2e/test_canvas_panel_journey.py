"""The canvas frame's click gate in a real browser (pivot P5, Task 26: the gate steps; audit G8-05, the lead's ruling 1 on S7 and the
review of #925). LEAD-SCHEDULED: written, collected and linted here, never run locally.

The console panel that mounts the canvas lands in S8, so these steps mount the frame the way the panel will, from a stand-in page served
on the console's own origin (``page.route``): an ``<iframe src="/canvas/frame">`` sandboxed ``allow-scripts allow-forms``, the
``primer.canvas.connect`` handshake with a ``MessageChannel``, and a ``state`` posted by the stand-in itself when the frame loads. S10
adds the panel's own steps to this file (Task 26) and may move these onto the panel.

``page.evaluate`` can itself grant user activation, so no step calls it before or during the input it measures: each Html element arms
its attack ``ARM_MS`` after it loads (any activation the setup granted has expired by then), the inputs are real ``page.mouse`` and
``page.keyboard`` events at coordinates read with ``bounding_box``, and the results are read with ``evaluate`` only afterwards.
"""

from __future__ import annotations

import json
import time
import uuid

from playwright.sync_api import Page

#: When an Html element arms its attack, after it loads (the frame's activation from the setup expires after about 5 s).
ARM_MS = 7500

HOST = """<!doctype html><html><head><meta charset="utf-8"><title>canvas gate stand-in</title></head><body>
<iframe id="cv" src="/canvas/frame" sandbox="allow-scripts allow-forms" style="width:900px;height:700px;border:0"></iframe>
<script>
window.received = [];
var STATE = __STATE__;
var frame = document.getElementById("cv");
frame.addEventListener("load", function () {
  var channel = new MessageChannel();
  channel.port1.onmessage = function (ev) { window.received.push(ev.data); };
  frame.contentWindow.postMessage({type: "primer.canvas.connect", protocol: 1}, "*", [channel.port2]);
  channel.port1.postMessage(STATE);
});
</script></body></html>"""

#: Takes the port the bootstrap holds (agent code shares its realm and can), so a test sees what the RENDERER lets through.
CAPTURE = ("var P = null, op = MessagePort.prototype.postMessage;"
           "MessagePort.prototype.postMessage = function (m) { P = this; return op.apply(this, arguments); };")
#: Once armed, steals the focus every 50 ms and emits.
THIEF = ("<input id=bi><script>" + CAPTURE + "var n = 0; setTimeout(function () { setInterval(function () { if (!P) return; n++;"
         "document.getElementById('bi').focus(); P.postMessage({type: 'emit', name: 'stolen', data: {n: n}}); }, 50); }, "
         + str(ARM_MS) + ");</script>")
#: Once armed, emits whenever it has the focus.
TAB_TARGET = ("<input id=ki><script>" + CAPTURE + "var n = 0; setTimeout(function () { setInterval(function () { if (!P) return;"
              "if (document.hasFocus()) { n++; P.postMessage({type: 'emit', name: 'tabbed', data: {n: n}}); } }, 50); }, "
              + str(ARM_MS) + ");</script>")
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


def _mount(page: Page, base_url: str, elements: dict, wait_for: str) -> float:
    """Serves the stand-in on the console's origin, opens it, waits for ``wait_for`` to be drawn; returns when the page loaded."""
    url = f"{base_url}/__canvas_gate_{uuid.uuid4().hex[:8]}.html"
    body = HOST.replace("__STATE__", json.dumps(_state(elements)).replace("<", "\\u003c"))
    page.route(url, lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body))
    page.goto(url)
    loaded = time.monotonic()
    page.frame_locator("#cv").locator(f"[data-cv-id={wait_for}]").wait_for(timeout=15_000)
    return loaded


def _until(page: Page, start: float, seconds: float) -> None:
    left = seconds - (time.monotonic() - start)
    if left > 0:
        page.wait_for_timeout(int(left * 1000))


def _click(page: Page, locator) -> None:
    box = locator.bounding_box()
    assert box is not None
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def _emits(page: Page) -> list[tuple[str, str]]:
    return [(m["element"], m["name"]) for m in page.evaluate("window.received") if m.get("type") == "event" and m.get("kind") == "emit"]


def test_a_looping_element_cannot_emit_after_its_activation_expires(page: Page, base_url: str) -> None:
    """G8-05: one click starts a loop that emits every 200 ms; at most 3 emits go out, however long it runs."""
    loaded = _mount(page, base_url, {"w": _html(LOOPER)}, "w")
    button = page.frame_locator("#cv").frame_locator("[data-cv-id=w] iframe").locator("#go")
    _until(page, loaded, 7)
    _click(page, button)
    page.wait_for_timeout(9_000)
    emits = _emits(page)
    assert 1 <= len(emits) <= 3 and all(e == ("w", "loop") for e in emits), emits


def test_a_frame_that_takes_the_focus_after_a_click_on_a_native_button_emits_nothing(page: Page, base_url: str) -> None:
    """The review of #925: a click on a native Button activates the renderer, and an Html element that was never touched and focuses
    itself gets nothing from that activation."""
    loaded = _mount(page, base_url, {"go": {"type": "Button", "props": {"label": "Go"}}, "b": _html(THIEF)}, "go")
    _until(page, loaded, ARM_MS / 1000 + 1)
    _click(page, page.frame_locator("#cv").locator("[data-cv-id=go]"))
    page.wait_for_timeout(2_500)
    assert _emits(page) == []
    assert any(m.get("type") == "event" and m.get("element") == "go" for m in page.evaluate("window.received")), "the click itself went out"


def test_a_tab_from_a_native_button_into_an_html_element_lets_it_emit_nothing(page: Page, base_url: str) -> None:
    """The review of #925: a Tab press on a native Button moves the focus into an Html element; the key press is the renderer's, so the
    element gets nothing from it."""
    loaded = _mount(page, base_url, {"go2": {"type": "Button", "props": {"label": "Go2"}}, "k": _html(TAB_TARGET)}, "go2")
    _until(page, loaded, 2)
    _click(page, page.frame_locator("#cv").locator("[data-cv-id=go2]"))   # focuses Go2; its activation expires before the Tab
    _until(page, loaded, ARM_MS / 1000 + 1.5)
    page.keyboard.press("Tab")
    page.wait_for_timeout(2_500)
    assert _emits(page) == []


def test_one_click_gives_its_activation_to_one_frame_only(page: Page, base_url: str) -> None:
    """The review of #925: the first Html element that passes the gate owns the activation, so a click spends ONE budget, in one frame.

    Known residual, accepted by the lead ("Lead rulings after the S7 review"): an element that steals the focus by script during another
    element's real click can own that activation if its message reaches the renderer first (CI saw exactly that). Both frames are the
    same canvas's agent code, so it gains no gesture the agent could not already get from that click. The gate does NOT promise that
    the clicked element wins; it promises that one element gets the activation's budget and that no element double-spends it. That is
    what this step asserts.
    """
    loaded = _mount(page, base_url, {"a": _html(CLICKED), "b": _html(THIEF)}, "a")
    _until(page, loaded, ARM_MS / 1000 + 1)
    _click(page, page.frame_locator("#cv").frame_locator("[data-cv-id=a] iframe").locator("#a"))
    page.wait_for_timeout(2_500)
    emits = _emits(page)
    owners = {element for element, _ in emits}
    assert len(owners) == 1, f"exactly one of A and B emits from the click's activation: {emits}"
    assert 1 <= len(emits) <= 3, f"the owner emits at most 3 times: {emits}"
