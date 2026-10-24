"""Html elements: the bootstrap, the narrowing media policy and the srcdoc (pivot P5, Task 17; design 8.6, plan Decision 10).

The bootstrap (``ui/canvas/html-bootstrap.js``) runs first inside each Html element's own opaque frame and gives the agent's code
``window.primer``. Its checks are advisory (audit G8-05: the agent's code shares its realm); the renderer's gate is the control and is
pinned in ``tests/ui/test_canvas_renderer_dom.py``. What is pinned here: the bootstrap's pure checks, that it takes the renderer's port
only from its parent in the exact shape (the lead's ruling 4 on S7, the Html frame's direction), that it queues until then, the media
policy an Html frame adds (``CV.metaCsp``) and the order of the srcdoc (``CV.htmlSrcdoc``: nothing from the agent before the policy and
the bootstrap), and that the frame shell carries the bootstrap as a string, never as code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.api._app_middleware import _UI_DIR
from primer.canvas.frame import build_frame_shell, js_string

UI = Path(__file__).resolve().parents[2] / "ui"
BOOTSTRAP = UI / "canvas" / "html-bootstrap.js"
RENDERER = UI / "canvas" / "renderer.js"


def _context(*sources: str):
    py_mini_racer = pytest.importorskip("py_mini_racer")
    context = py_mini_racer.MiniRacer()
    context.eval("var window = globalThis;")
    for source in sources:
        context.eval(source)
    return context


@pytest.fixture
def ctx():
    context = _context(BOOTSTRAP.read_text(encoding="utf-8"))
    yield context
    context.close()


@pytest.fixture
def renderer_ctx():
    context = _context(RENDERER.read_text(encoding="utf-8"))
    yield context
    context.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


# --- the bootstrap's pure checks ------------------------------------------------------------------------------------------------------


def test_emit_needs_user_activation(ctx):                                   # Review Focus 4
    assert js(ctx, 'PB_canAct({isActive: false}, "visible")') is False
    assert js(ctx, 'PB_canAct({isActive: true}, "hidden")') is False
    assert js(ctx, 'PB_canAct({isActive: true}, "visible")') is True
    assert js(ctx, 'PB_canAct(undefined, "visible")') is False
    assert js(ctx, 'PB_canAct({isActive: 1}, "visible")') is False


def test_emit_names_and_data_are_checked(ctx):
    assert js(ctx, 'PB_validateEmit("picked", {"n": 1})') is None
    assert js(ctx, 'PB_validateEmit("a.b:c-d_e", null)') is None
    assert js(ctx, 'PB_validateEmit("/cmd", {})') == "an emit name is 1 to 64 letters, digits, _ . : or -, and does not start with /"
    assert js(ctx, 'PB_validateEmit("", {})') is not None and js(ctx, 'PB_validateEmit("x".repeat(65), {})') is not None
    assert js(ctx, 'PB_validateEmit("big", {"pad": "x".repeat(5000)})') == "emit data is at most 4096 bytes of JSON"
    assert js(ctx, 'PB_validateEmit("big", "é".repeat(2100))') == "emit data is at most 4096 bytes of JSON", "bytes, not characters"
    assert js(ctx, '(function () { var v = {}; v.v = v; return PB_validateEmit("cyclic", v); })()') == "emit data must be JSON"


def test_open_link_takes_http_or_https_only(ctx):
    assert js(ctx, 'PB_validateUrl("https://a.example/x?y#z")') is None
    assert js(ctx, 'PB_validateUrl("http://localhost:9000/")') is None
    for bad in ("javascript:alert(1)", "data:text/html,x", "https://u:p@a.example/", "ftp://a.example", "https://a.example/a b", "/relative"):
        assert js(ctx, f"PB_validateUrl({json.dumps(bad)})") == "primer.openLink takes an http or https URL", bad


def test_the_port_is_taken_only_from_the_parent_in_the_exact_shape(ctx):
    """The lead's ruling 4 on S7, the Html frame's side of the handshake."""
    ctx.eval("var P = {}, SELF = {}, OTHER = {}, PORT = {};")
    ok = '{type: "primer.html.connect", element: "widget"}'
    assert js(ctx, f"PB_acceptsConnect({ok}, P, P, SELF, [PORT])") is True
    assert js(ctx, f"PB_acceptsConnect({ok}, OTHER, P, SELF, [PORT])") is False
    assert js(ctx, f"PB_acceptsConnect({ok}, SELF, P, SELF, [PORT])") is False, "the agent's own window.postMessage"
    assert js(ctx, f"PB_acceptsConnect({ok}, P, P, SELF, [])") is False
    assert js(ctx, 'PB_acceptsConnect({type: "primer.html.connect", element: "Bad id"}, P, P, SELF, [PORT])') is False
    assert js(ctx, 'PB_acceptsConnect({type: "primer.canvas.connect", protocol: 1}, P, P, SELF, [PORT])') is False


def test_the_renderers_messages_are_checked(ctx):
    assert js(ctx, 'PB_validateHostMessage({type: "value", value: {a: [1, 2]}})') == {"type": "value", "value": {"a": [1, 2]}}
    assert js(ctx, 'PB_validateHostMessage({type: "theme", theme: {"--cv-bg": "#fff", "--x": "red", "--cv-y": 3}})') == {
        "type": "theme", "theme": {"--cv-bg": "#fff"},
    }
    for bad in ('{type: "value", value: "x".repeat(2000)}', '{type: "state"}', "null", '"value"', '{type: "theme", theme: []}'):
        assert js(ctx, f"PB_validateHostMessage({bad})") is None, bad


# --- the bootstrap's wiring, on a fake window -----------------------------------------------------------------------------------------

FAKE_FRAME = r"""
var RENDERER_WINDOW = {name: "renderer"};
var listeners = {};
var dispatched = [];
var styleProps = {};
var warned = [];
var window = {
  RTCPeerConnection: function () {}, webkitRTCPeerConnection: function () {}, RTCDataChannel: function () {},
  console: {warn: function (m) { warned.push(m); }},
  parent: RENDERER_WINDOW,
  navigator: {userActivation: {isActive: false}},
  document: {visibilityState: "visible", documentElement: {scrollHeight: 120, style: {setProperty: function (k, v) { styleProps[k] = v; }}}},
  addEventListener: function (type, fn, capture) { (listeners[type] = listeners[type] || []).push(fn); },
  dispatchEvent: function (ev) { dispatched.push(ev); },
  CustomEvent: function (type, init) { this.type = type; this.detail = init.detail; }
};
function deliver(data, source, ports) {
  var stopped = false;
  var ev = {data: data, source: source, ports: ports || [], stopImmediatePropagation: function () { stopped = true; }};
  (listeners.message || []).forEach(function (fn) { if (!stopped) fn(ev); });
  return stopped;
}
function Port() { this.sent = []; this.onmessage = null; }
Port.prototype.postMessage = function (m) { this.sent.push(m); };
"""


@pytest.fixture
def frame():
    context = _context()
    context.eval(FAKE_FRAME)
    context.eval(BOOTSTRAP.read_text(encoding="utf-8"))
    yield context
    context.close()


def test_the_bootstrap_queues_until_its_parent_offers_a_port(frame):
    frame.eval("window.primer.setValue({step: 1}); var A = new Port(), B = new Port();")
    assert js(frame, 'deliver({type: "primer.html.connect", element: "w"}, {name: "elsewhere"}, [A])') is False
    assert js(frame, 'deliver({type: "primer.html.connect", element: "w"}, window, [A])') is False, "its own window"
    assert js(frame, "A.sent") == []
    assert js(frame, 'deliver({type: "primer.html.connect", element: "w"}, RENDERER_WINDOW, [B])') is True
    assert js(frame, "B.sent") == [{"type": "set_value", "value": {"step": 1}}], "the queue goes out on the port it took"
    assert js(frame, 'deliver({type: "primer.html.connect", element: "w"}, RENDERER_WINDOW, [A])') is False, "first offer wins"
    frame.eval("window.primer.setValue(2);")
    assert js(frame, "A.sent.length") == 0 and js(frame, "B.sent[B.sent.length - 1]") == {"type": "set_value", "value": 2}


def test_emit_copy_and_open_link_throw_without_a_click(frame):
    frame.eval('var B = new Port(); deliver({type: "primer.html.connect", element: "w"}, RENDERER_WINDOW, [B]);')
    for call, fn in (('window.primer.emit("auto", {})', "emit"), ('window.primer.copy("x")', "copy"), ('window.primer.openLink("https://a.example/")', "openLink")):
        message = js(frame, f"(function () {{ try {{ {call}; return null; }} catch (e) {{ return e.message; }} }})()")
        assert message == f"primer.{fn} needs a click or a key press first"
    assert js(frame, "B.sent") == []
    frame.eval('window.navigator.userActivation.isActive = true; window.primer.emit("clicked", {n: 1, f: function () {}});')
    assert js(frame, "B.sent") == [{"type": "emit", "name": "clicked", "data": {"n": 1}}], "data is sent as its JSON"
    frame.eval('window.document.visibilityState = "hidden";')
    assert js(frame, '(function () { try { window.primer.emit("x"); return null; } catch (e) { return e.message; } })()') == (
        "primer.emit needs a click or a key press first"
    )


def test_the_bootstrap_removes_webrtc_before_the_agents_code(frame):
    """The lead's WebRTC ruling on #925: defence in depth only (the policy's ``webrtc 'block'`` is the rule where a browser knows it)."""
    assert js(frame, "[typeof window.RTCPeerConnection, typeof window.webkitRTCPeerConnection, typeof window.RTCDataChannel]") == [
        "undefined", "undefined", "undefined"]
    frame.eval("window.RTCPeerConnection = function () {};")
    assert js(frame, "typeof window.RTCPeerConnection") == "undefined", "the agent's code cannot put it back on the window"


def test_a_dropped_call_is_heard_in_the_page(frame):
    """The review of #925: the renderer answers a call its gate dropped with ``{type: "error"}``; the page gets a warning and a
    ``primer:error`` event."""
    frame.eval('var B = new Port(); deliver({type: "primer.html.connect", element: "w"}, RENDERER_WINDOW, [B]);'
               'B.onmessage({data: {type: "error", message: "primer.emit was not sent"}}); B.onmessage({data: {type: "error", message: 5}});')
    assert js(frame, "warned") == ["primer.emit was not sent"]
    assert js(frame, "dispatched.map(function (e) { return [e.type, e.detail]; })") == [["primer:error", "primer.emit was not sent"]]


def test_the_renderers_value_and_theme_reach_the_page(frame):
    frame.eval('var B = new Port(); deliver({type: "primer.html.connect", element: "w"}, RENDERER_WINDOW, [B]);')
    frame.eval('B.onmessage({data: {type: "value", value: [1, 2]}}); B.onmessage({data: {type: "nope"}});'
               'B.onmessage({data: {type: "theme", theme: {"--cv-fg": "#123", "--evil": "x"}}});')
    assert js(frame, "window.primer.value") == [1, 2]
    assert js(frame, "dispatched.map(function (e) { return [e.type, e.detail]; })") == [["primer:value", [1, 2]]]
    assert js(frame, "styleProps") == {"--cv-fg": "#123"}


# --- the media policy of an Html frame and its srcdoc ----------------------------------------------------------------------------------


def test_media_policy_none_restricts_html_media(renderer_ctx):             # Review Focus 4
    assert js(renderer_ctx, 'CV.metaCsp({media_policy: "none", media_allowlist: []}, "http://h:9000")') == \
        "img-src data: blob: http://h:9000; media-src data: blob: http://h:9000; font-src data:; webrtc 'block'"
    doc = js(renderer_ctx, 'CV.htmlSrcdoc("<img src=x>", {settings: {media_policy: "none", media_allowlist: []}, ownOrigin: "http://h:9000", theme: {}, bootstrap: "B"})')
    assert doc.index('http-equiv="Content-Security-Policy"') < doc.index("<script>B</script>") < doc.index("<img src=x>")


def test_any_https_adds_no_media_policy_only_webrtc_block(renderer_ctx):
    """The plan's "any-https adds no meta policy", amended by the lead's WebRTC ruling on #925: under any-https the header's media rule
    holds and the Html frame's policy carries only ``webrtc 'block'``."""
    assert js(renderer_ctx, 'CV.metaCsp({media_policy: "any-https", media_allowlist: []}, "http://h:9000")') == "webrtc 'block'"
    doc = js(renderer_ctx, 'CV.htmlSrcdoc("<p>hi</p>", {settings: {media_policy: "any-https", media_allowlist: []}, ownOrigin: "", theme: {}, bootstrap: "B"})')
    assert doc.startswith('<!doctype html><html><head><meta charset="utf-8">'
                          '<meta http-equiv="Content-Security-Policy" content="webrtc \'block\'"><style>')
    assert doc.endswith("</style><script>B</script></head><body><p>hi</p></body></html>")


@pytest.mark.parametrize("policy", ["any-https", "allowlist", "none", "strange"])
def test_every_html_frame_blocks_webrtc(renderer_ctx, policy):
    """The lead's WebRTC ruling on #925: ``webrtc 'block'`` under every media policy (a browser that does not know it ignores it)."""
    csp = js(renderer_ctx, f'CV.metaCsp({{media_policy: "{policy}", media_allowlist: ["a.example.com"]}}, "")')
    assert csp.split("; ")[-1] == "webrtc 'block'" and csp.count("webrtc") == 1


def test_the_allowlist_policy_names_the_hosts_and_the_own_origin(renderer_ctx):
    assert js(renderer_ctx, 'CV.metaCsp({media_policy: "allowlist", media_allowlist: ["a.example.com", "*.cdn.example"]}, "http://h:9000")') == (
        "img-src data: blob: http://h:9000 https://a.example.com https://*.cdn.example; "
        "media-src data: blob: http://h:9000 https://a.example.com https://*.cdn.example; "
        "font-src data: https://a.example.com https://*.cdn.example; webrtc 'block'"
    )
    assert js(renderer_ctx, 'CV.metaCsp({media_policy: "allowlist", media_allowlist: []}, "")') == (
        "img-src data: blob:; media-src data: blob:; font-src data:; webrtc 'block'"
    )


def test_nothing_hostile_reaches_the_policy(renderer_ctx):
    """The origin and the hosts are written into an attribute value: one that is not an origin or a host name is left out."""
    csp = js(renderer_ctx, 'CV.metaCsp({media_policy: "allowlist", media_allowlist: ["a.example\\" onload=\\"x", "b.example; script-src *"]}, '
                           '"http://h:9000\\"><script>x</script>")')
    assert csp == "img-src data: blob:; media-src data: blob:; font-src data:; webrtc 'block'"
    assert js(renderer_ctx, 'CV.metaCsp({media_policy: "strange", media_allowlist: []}, "")') == (
        "img-src data: blob:; media-src data: blob:; font-src data:; webrtc 'block'"
    ), "a policy the renderer does not know is the strictest"


def test_the_srcdoc_puts_the_policy_theme_and_bootstrap_before_the_agents_html(renderer_ctx):
    agent = "</head><meta http-equiv=\"Content-Security-Policy\" content=\"img-src *\"><script>evil()</script>"
    opts = {"settings": {"media_policy": "none", "media_allowlist": []}, "ownOrigin": "",
            "theme": {"--cv-fg": "#111", "--cv-bg": "red;}"}, "bootstrap": "BOOT"}
    doc = js(renderer_ctx, f"CV.htmlSrcdoc({json.dumps(agent)}, {json.dumps(opts)})")
    head = doc[: doc.index("<body>")]
    assert head.startswith('<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="img-src data: blob:;')
    assert "--cv-fg:#111;" in head and "red;}" not in doc
    assert head.endswith("<script>BOOT</script></head>")
    assert doc.endswith("<body>" + agent + "</body></html>")


def test_a_bootstrap_that_could_end_its_script_is_refused(renderer_ctx):
    for bad in ("x</script><script>y", "x</SCRIPT >", "<!-- x"):
        opts = {"settings": {"media_policy": "none", "media_allowlist": []}, "bootstrap": bad}
        assert js(renderer_ctx, f"CV.htmlSrcdoc('<p>x</p>', {json.dumps(opts)})") is None, bad


def test_the_bootstrap_never_names_what_would_end_its_script():
    source = BOOTSTRAP.read_text(encoding="utf-8").lower()
    assert "</script" not in source and "<!--" not in source


# --- the shell carries the bootstrap as a string --------------------------------------------------------------------------------------


def test_the_shell_inlines_the_bootstrap():
    assert b"PB_canAct" in build_frame_shell(_UI_DIR).body


def test_the_shell_carries_the_bootstrap_as_a_string_never_as_code():
    body = build_frame_shell(_UI_DIR).body.decode("utf-8")
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "window.CV_BOOTSTRAP = " + js_string(source) + ";" in body
    assert source not in body, "the bootstrap's code is never in the shell as code: it runs only inside an Html frame"
    assert body.index("window.CV_BOOTSTRAP") < body.index("window.CV.boot(document, window)"), "the renderer reads it when it boots"


def test_the_string_form_round_trips_and_cannot_close_its_script(renderer_ctx):
    hostile = "a</script><script>alert(1)</script><!--    é \U0001f600"
    literal = js_string(hostile)
    assert "<" not in literal and " " not in literal and " " not in literal
    assert js(renderer_ctx, literal) == hostile
