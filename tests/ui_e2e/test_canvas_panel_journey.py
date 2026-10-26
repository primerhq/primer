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

S10 (Task 26) adds the PANEL's own steps below, drawn through ``POST .../canvas/ops`` (no model needed; the install runs with auth
disabled, so ``by`` is ``system``): the panel shows a session's canvas and a click reaches the server; Hide/Show live in the URL; the
split resizes by the keyboard arrows (the handle has no pointer drag); the frame is an opaque origin with no ``allow-same-origin``
anywhere; an Html element's load-time emit is refused until a real click, and the page's only console error is the accepted WebRTC
residual; strict mode holds an Html element behind a Run button; and a phone uses a header chip and a top sheet instead of the split.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest

from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests.ui_e2e._scripted_session import seed_scripted_agent, start_session
from tests.ui_e2e._shell_helpers import boot_mobile_shell, run_verb
from tests.ui_e2e._studio_helpers import open_session_in_studio

#: How long a step waits, calling nothing, after its last in-frame call: past Chromium's 5 s transient activation.
SETTLE_MS = 6_500

# Frame-document render budget (ms): the Html srcdoc frame must load,
# parse and render within it. ~1s locally; the CI serial runner has
# twice exceeded the old 20s literal under load (heads 6b8ef18a8 /
# 6801d41e4, jobs 121312222154 / 121338479538: the panel settled at
# rev 1 with an empty frame iframe, and the CSP pin's console was
# empty because the document never loaded in time). The bridge logic
# is not in question - the strict-mode, isolation and activation
# journeys in this file pass in the same CI runs; this bounds
# document load under runner load only.
FRAME_RENDER_BUDGET_MS = 60_000

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


# ---------------------------------------------------------------------------
# The panel's own steps (S10, Task 26): the canvas drawn through POST .../canvas/ops (no model
# needed) and the session opened in its studio. These run against the real console panel, not the
# stand-in page above, so they need no measured window: the only gate they assert is an Html element's.
# ---------------------------------------------------------------------------

PHONE = {"width": 390, "height": 844}

PLAYLIST = (
    "root = Column([genre, play])\n"
    'genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")\n'
    'play = Button("Play", "primary")\n'
)

# Eight Text elements (the root not counted), so the phone chip reads "Canvas \u00b7 8".
EIGHT = "root = Column([a, b, c, d, e, f, g, h])\n" + "".join(f"{x} = Text(\"{x}\")\n" for x in "abcdefgh")

# One Html element, a NON-ROOT child (so its data-cv-id is its own variable, "viz", not the root's "root"): a script that
# emits on load (no user activation, so refused) and a button whose click emits (allowed).
_HTML_BODY = (
    "<button id=go style='width:200px;height:60px' "
    "onclick=\"primer.emit('clicked', {n: 1})\">Go</button>"
    "<script>primer.emit('auto', {});</script>"
)
HTML_SOURCE = 'root = Column([viz])\nviz = Html("""' + _HTML_BODY + '""")\n'

# The plan's G8-06 ruling pin (plan lines 30-32, binding on Tasks 12-27): an Html element whose srcdoc loads a script from a
# host that is NOT in the frame's script-src (primer/canvas/frame.py: the four CDNs). The host is evil.example (an RFC 2606
# reserved domain), never an allowed source, so the frame's CSP blocks the script and the console logs the violation.
CSP_SCRIPT_SOURCE = 'root = Column([viz])\nviz = Html("""<script src="https://evil.example/ping.js"></script>""")\n'


def _canvas_frame(page: Page, *, timeout: int = 20_000):
    """The canvas frame (the ``nv-canvas-frame`` iframe), found by its URL.

    The iframe element renders before the frame's document has navigated (the
    frame shell is a fresh page load; the frame reads as an empty URL until the
    navigation commits), so poll until the frame shows up with its URL rather
    than reading ``page.frames`` once.
    """
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for f in page.frames:
            if "/canvas/frame" in f.url:
                return f
        page.wait_for_timeout(250)
    raise AssertionError(f"no canvas frame among {[f.url for f in page.frames]}")


def _draw(client: httpx.Client, wid: str, sid: str, source: str) -> str:
    canvas = f"/v1/workspaces/{wid}/sessions/{sid}/canvas"
    r = client.post(f"{canvas}/ops", json={"source": source})
    assert r.status_code == 200, r.text
    return canvas


def _view(client: httpx.Client, canvas: str) -> dict:
    return client.get(canvas).json()


def _wait_event_seq(client: httpx.Client, canvas: str, n: int, *, timeout_s: float = 30.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        view = _view(client, canvas)
        if view["event_seq"] >= n:
            return view
        time.sleep(0.5)
    pytest.fail(f"event_seq never reached {n}: {_view(client, canvas)}")


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


# ---------------------------------------------------------------------------
# The panel's own journeys (S10, Task 26): the real console panel, drawn through ops.
# ---------------------------------------------------------------------------


@pytest.mark.ui_e2e
def test_the_panel_shows_itself_and_a_click_reaches_the_server(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    """The panel shows a session's canvas; a click on its Play button reaches the server and the transcript shows the chip."""
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="cvn1")
    registry.register(ids["model_name"], [Rule(emit_text="ok")])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "Draw the playlist", auto_start=False)
        canvas = _draw(client, ids["workspace"], sid, PLAYLIST)
    open_session_in_studio(page, console_url, ids["workspace"], sid)
    expect(page.get_by_test_id("nv-canvas")).to_be_visible(timeout=20_000)
    frame = page.frame_locator('[data-testid="nv-canvas-frame"]')
    play = frame.locator('[data-cv-id="play"]')
    expect(play).to_be_visible(timeout=20_000)
    play.click()
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        _wait_event_seq(client, canvas, 1)
    expect(page.get_by_text("system clicked Play", exact=True)).to_be_visible(timeout=20_000)


@pytest.mark.ui_e2e
def test_hide_and_show_live_in_the_url(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path):
    """Hide Canvas and Show Canvas move the ``canvas=`` key in the URL, and a reload keeps the state.

    The verb rows are clicked by their exact ``data-row-key`` (``v:canvas.hide`` /
    ``v:canvas.show``), not ``run_verb``'s ``.first``: after the reload the palette
    re-renders as its polled state settles and the shared-testid ``.first`` can race
    that (the known palette2 flake family); the per-row handle is the stable target.
    """
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="cvn2")
    registry.register(ids["model_name"], [Rule(emit_text="ok")])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "Draw the playlist", auto_start=False)
        _draw(client, ids["workspace"], sid, PLAYLIST)
    open_session_in_studio(page, console_url, ids["workspace"], sid)
    expect(page.get_by_test_id("nv-canvas")).to_be_visible(timeout=20_000)
    run_verb(page, "Hide Canvas", row_key="v:canvas.hide")
    expect(page.get_by_test_id("nv-canvas")).to_be_hidden(timeout=20_000)
    assert "canvas=closed" in page.url, page.url
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_test_id(f"nv-session-doc:{sid}")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-canvas")).to_be_hidden(timeout=20_000)
    run_verb(page, "Show Canvas", row_key="v:canvas.show")
    expect(page.get_by_test_id("nv-canvas")).to_be_visible(timeout=20_000)
    assert "canvas=open" in page.url, page.url


@pytest.mark.ui_e2e
def test_the_split_resizes(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path):
    """The canvas/transcript split resizes by the keyboard arrows (5% each) and a double-click resets it to 50.

    The resize handle (``nv-canvas-resize``) has no pointer drag; the plan's "dragging" step is the keyboard path the handle implements.
    """
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="cvn3")
    registry.register(ids["model_name"], [Rule(emit_text="ok")])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "Draw the playlist", auto_start=False)
        _draw(client, ids["workspace"], sid, PLAYLIST)
    open_session_in_studio(page, console_url, ids["workspace"], sid)
    handle = page.get_by_test_id("nv-canvas-resize")
    expect(handle).to_be_visible(timeout=20_000)
    start = int(handle.get_attribute("aria-valuenow"))
    handle.click()  # focus the handle (tabIndex=0); it has no onClick, so a single click only focuses it
    page.keyboard.press("ArrowDown")
    assert int(page.get_by_test_id("nv-canvas-resize").get_attribute("aria-valuenow")) == start - 5
    page.get_by_test_id("nv-canvas-resize").dblclick()
    assert int(page.get_by_test_id("nv-canvas-resize").get_attribute("aria-valuenow")) == 50


@pytest.mark.ui_e2e
def test_isolation(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path):
    """The canvas frame is an opaque origin (origin ``null``, no readable cookie) and no iframe in the page is ``allow-same-origin``."""
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="cvn4")
    registry.register(ids["model_name"], [Rule(emit_text="ok")])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "Draw the playlist", auto_start=False)
        _draw(client, ids["workspace"], sid, PLAYLIST)
    open_session_in_studio(page, console_url, ids["workspace"], sid)
    expect(page.get_by_test_id("nv-canvas-frame")).to_be_visible(timeout=20_000)
    cv = _canvas_frame(page)
    origin = None
    for _ in range(40):
        try:
            origin = cv.evaluate("window.origin")
            break
        except Exception:  # noqa: BLE001 - the frame is still loading
            page.wait_for_timeout(250)
    assert origin == "null", f"the canvas frame is an opaque origin, got origin {origin!r}"
    cookie = cv.evaluate("() => { try { return document.cookie; } catch (e) { return 'threw:' + (e && e.name); } }")
    assert cookie == "" or cookie.startswith("threw:"), f"document.cookie should be empty or throw, got {cookie!r}"
    sandboxes = page.evaluate("Array.from(document.querySelectorAll('iframe')).map((f) => f.getAttribute('sandbox') || '')")
    assert not any("allow-same-origin" in s for s in sandboxes), sandboxes


@pytest.mark.ui_e2e
def test_html_emits_need_a_click(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
    console_messages, assert_no_console_errors_fn,
):
    """An Html element's load-time emit is refused (no user activation); a click on its button records the emit.

    G8-05: the page's only console errors are the accepted WebRTC residual (Chromium logs the unrecognized ``webrtc``
    directive) and the refused load-time emit itself: the host's ``primer.emit`` throws in the agent's srcdoc, and the
    uncaught exception is the refusal's observable (the assertion on ``event_seq == 0`` is the pin). Both lines are
    passed to ``ignore_patterns``.
    """
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="cvn5")
    registry.register(ids["model_name"], [Rule(emit_text="ok")])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "Draw the widget", auto_start=False)
        canvas = _draw(client, ids["workspace"], sid, HTML_SOURCE)
    open_session_in_studio(page, console_url, ids["workspace"], sid)
    frame = page.frame_locator('[data-testid="nv-canvas-frame"]')
    html_frame = frame.frame_locator('[data-cv-id="viz"] iframe')
    expect(html_frame.locator("#go")).to_be_visible(timeout=FRAME_RENDER_BUDGET_MS)
    page.wait_for_timeout(3_000)  # the load-time emit had no user activation
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        assert _view(client, canvas)["event_seq"] == 0, "the load-time emit was refused (no user activation)"
    html_frame.locator("#go").click()
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        view = _wait_event_seq(client, canvas, 1)
    assert any(e["kind"] == "emit" and e.get("name") == "clicked" for e in view["events"]), view["events"]
    assert_no_console_errors_fn(
        console_messages,
        ignore_patterns=[
            r"Unrecognized Content-Security-Policy directive 'webrtc'",
            r"primer\.emit needs a click or a key press first",
        ],
    )


@pytest.mark.ui_e2e
def test_strict_mode_needs_run(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path):
    """With ``strict_mode`` on, an Html element stays inert behind a Run button; a click on it runs the content."""
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="cvn6")
    registry.register(ids["model_name"], [Rule(emit_text="ok")])
    settings = None
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "Draw the widget", auto_start=False)
        canvas = _draw(client, ids["workspace"], sid, HTML_SOURCE)
        settings = client.get("/v1/canvas_settings").json()
        r = client.put("/v1/canvas_settings", json={**settings, "strict_mode": True})
        assert r.status_code == 200, r.text
    try:
        open_session_in_studio(page, console_url, ids["workspace"], sid)
        frame = page.frame_locator('[data-testid="nv-canvas-frame"]')
        run_btn = frame.locator('[data-cv="html-run"]')
        expect(run_btn).to_be_visible(timeout=FRAME_RENDER_BUDGET_MS)
        run_btn.click()
        html_frame = frame.frame_locator('[data-cv-id="viz"] iframe')
        expect(html_frame.locator("#go")).to_be_visible(timeout=FRAME_RENDER_BUDGET_MS)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            r = client.put("/v1/canvas_settings", json=settings)
            assert r.status_code == 200, r.text


@pytest.mark.ui_e2e
def test_the_phone_uses_a_chip_and_a_top_sheet(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path):
    """At 390x844 the session header shows a chip naming the element count; a tap opens the top sheet; closing it drops ``canvas=``."""
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="cvn7")
    registry.register(ids["model_name"], [Rule(emit_text="ok")])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "Draw the list", auto_start=False)
        _draw(client, ids["workspace"], sid, EIGHT)
    page.set_viewport_size(PHONE)
    boot_mobile_shell(page, f"{console_url}#/w/{ids['workspace']}?doc=session:{sid}")
    chip = page.get_by_test_id("nv-canvas-chip")
    expect(chip).to_be_visible(timeout=20_000)
    expect(chip).to_have_text("Canvas \u00b7 8", timeout=20_000)
    chip.click()
    expect(page.get_by_test_id("nv-canvas-sheet")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-canvas-frame")).to_be_visible(timeout=20_000)
    page.get_by_test_id("nv-canvas-sheet-close").click()
    expect(page.get_by_test_id("nv-canvas-sheet")).to_be_hidden(timeout=20_000)
    assert "canvas=" not in page.url, page.url


@pytest.mark.ui_e2e
def test_a_disallowed_host_script_trips_the_frame_csp(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
    console_messages,
):
    """An Html element whose srcdoc loads a script from a host not in script-src trips the frame's CSP (the G8-06 pin).

    The plan's G8-06 ruling (plan lines 30-32, binding on Tasks 12-27): pin that every Html srcdoc frame inherits the CSP
    by a journey step that loads a script from a host that is not allowed and asserts a CSP violation. This is the
    ENFORCEMENT pin (the frame actually refuses the script); the string pin (tests/canvas/test_frame.py) and the header pin
    (the e2e header journey) are not sufficient. The console's CSP-violation message names the disallowed host.
    """
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="cvn8")
    registry.register(ids["model_name"], [Rule(emit_text="ok")])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "Draw the widget", auto_start=False)
        _draw(client, ids["workspace"], sid, CSP_SCRIPT_SOURCE)
    open_session_in_studio(page, console_url, ids["workspace"], sid)
    frame = page.frame_locator('[data-testid="nv-canvas-frame"]')
    expect(frame.locator('[data-cv-id="viz"]')).to_be_visible(timeout=FRAME_RENDER_BUDGET_MS)
    # The srcdoc frame parses the disallowed <script> and the CSP check logs the refusal; poll the console for it.
    # The CSP violation is only logged once the srcdoc document has loaded and attempted the
    # disallowed script, so the poll budget must cover the document load under runner load.
    deadline = time.time() + 45.0
    csp = []
    while time.time() < deadline and not csp:
        csp = [m for m in console_messages if "Content Security Policy" in m["text"] and "evil.example" in m["text"]]
        if not csp:
            page.wait_for_timeout(200)
    assert csp, f"no CSP violation for the disallowed host; console was {[m['text'] for m in console_messages]}"
