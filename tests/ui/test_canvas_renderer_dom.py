"""The canvas renderer's DOM side, ``CV.boot`` (pivot P5, Tasks 16 and 17), on the fake DOM of ``tests/ui/_canvas_dom.py`` in MiniRacer.

What the lead's rulings on S7 pin here, through the real wiring rather than the pure helpers:

1. (G8-05) an Html emit, copy or open_link passes only while the RENDERER's ``navigator.userActivation.isActive`` holds, the element's
   frame is the renderer's ``activeElement`` and the page is visible, at most 3 per activation (a stubbed ``userActivation``);
3. media is keyed on ``file:<path>``: a re-minted URL never restarts a playing Audio (nor reloads an Image);
4. every message handler checks who sent it and its shape: the console's handshake only from ``window.parent``, the first wins, every
   other window message (an Html frame's ``parent.postMessage``) is dropped, and an Html element is heard only on the port it was given;
5. an Html element renders inert with a Run button unless the state message's ``run_html`` is true (and strict mode is off);
6. everything the renderer puts in the DOM is a text node, a tag and an attribute from short lists, or a theme value the validators
   passed.

The browser journeys (Task 26: a looping element cannot emit after the activation expires; CSP inheritance into an Html frame) are
lead-scheduled.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._canvas_dom import canvas_dom_context, js

ALLOWED_TAGS = {"MAIN", "DIV", "SECTION", "H1", "H2", "H3", "P", "PRE", "CODE", "STRONG", "EM", "BR", "HR", "BUTTON", "SPAN", "LABEL",
                "SELECT", "OPTION", "FIELDSET", "LEGEND", "INPUT", "IMG", "AUDIO", "IFRAME"}
ALLOWED_ATTRIBUTES = {"id", "type", "data-cv-id", "data-cv", "sandbox", "title", "aria-busy", "aria-label", "inert"}
HOSTILE = '<img src=x onerror="alert(1)">'


@pytest.fixture
def ctx():
    context = canvas_dom_context()
    yield context
    context.close()


def els(**elements) -> str:
    """A JS object literal of elements under a root Column holding them all, in order."""
    out = {"root": {"type": "Column", "props": {}, "children": list(elements)}}
    out.update(elements)
    return json.dumps(out)


def state(ctx, elements: str, **extra) -> None:
    ctx.eval(f"FD.state({elements}, {json.dumps(extra)});")


def events(ctx):
    return [m for m in js(ctx, "FD.sent()") if m["type"] != "ready"]


# --- ruling 4: who may speak ---------------------------------------------------------------------------------------------------------


def test_the_handshake_is_taken_only_from_the_parent_and_the_first_wins(ctx):
    ctx.eval("var C1 = new FD.MessageChannel(); "
             "FD.deliver({type: 'primer.canvas.connect', protocol: 1}, {name: 'html frame'}, [C1.port1]);")
    assert js(ctx, "R.connected()") is False, "a handshake from a window that is not the parent"
    ctx.eval("FD.deliver({type: 'primer.canvas.connect', protocol: 2}, FD.consoleWindow, [C1.port1]);")
    ctx.eval("FD.deliver({type: 'primer.canvas.connect', protocol: 1}, FD.consoleWindow, []);")
    assert js(ctx, "R.connected()") is False, "the wrong shape, or no port"
    ctx.eval("var first = FD.connect();")
    assert js(ctx, "R.connected()") is True and js(ctx, "FD.sent()") == [{"type": "ready", "protocol": 1}]
    ctx.eval("var C2 = new FD.MessageChannel(); C2.port2.onmessage = function () {}; "
             "FD.deliver({type: 'primer.canvas.connect', protocol: 1}, FD.consoleWindow, [C2.port1]);")
    assert js(ctx, "C2.port1.onmessage") is None, "a later offer is ignored"
    ctx.eval(f"C2.port2.postMessage({{type: 'state', rev: 1, elements: {els(t={'type': 'Text', 'props': {'text': 'from the second port'}})}, "
             "values: {}, settings: {media_policy: 'none', media_allowlist: [], strict_mode: false}});")
    assert js(ctx, "FD.root.textContent") == ""


def test_window_messages_after_the_handshake_are_dropped(ctx):
    """An Html frame's ``parent.postMessage`` is open: it reaches the renderer's window and is never acted on."""
    ctx.eval("FD.connect();")
    msg = ("{type: 'state', rev: 1, elements: " + els(t={"type": "Text", "props": {"text": "injected"}}) +
           ", values: {}, settings: {media_policy: 'any-https', media_allowlist: [], strict_mode: false}}")
    ctx.eval(f"FD.deliver({msg}, {{name: 'html frame'}}, []); FD.deliver({msg}, FD.consoleWindow, []);")
    assert js(ctx, "FD.root.textContent") == ""


def test_a_malformed_console_message_is_dropped(ctx):
    ctx.eval("FD.connect(); FD.post({type: 'state', rev: 'x'}); FD.post({type: 'busy', element: 'A b', on: true}); FD.post('state');")
    assert js(ctx, "FD.root.childNodes.length") == 0 and events(ctx) == []


# --- drawing; ruling 6 ---------------------------------------------------------------------------------------------------------------


EVERYTHING = els(
    head={"type": "Text", "props": {"text": "Hi **" + HOSTILE + "** _x_ `y`\nline two", "style": "h2"}},
    card={"type": "Card", "props": {"title": HOSTILE}, "children": ["row"]},
    row={"type": "Row", "props": {"gap": 4, "align": "end", "wrap": False}, "children": ["go", "pick", "many"]},
    go={"type": "Button", "props": {"label": HOSTILE, "style": "danger"}},
    pick={"type": "Select", "props": {"label": HOSTILE, "options": [["a", HOSTILE], ["b", "Beta"]], "value": "b"}},
    many={"type": "Select", "props": {"label": "Many", "options": ["x", "y"], "multiple": True}},
    rule={"type": "Divider", "props": {}},
    pic={"type": "Image", "props": {"src": "https://img.example/a.png", "alt": HOSTILE, "height": 100}},
    song={"type": "Audio", "props": {"src": "file:a.mp3", "title": HOSTILE}},
    docs={"type": "Link", "props": {"label": HOSTILE, "href": "https://docs.example/a"}},
    code={"type": "Text", "props": {"text": HOSTILE, "style": "code"}},
    widget={"type": "Html", "props": {"html": "<b>agent</b>"}},
)


def test_a_state_draws_every_element_with_text_nodes_and_known_tags_only(ctx):
    ctx.eval("FD.connect();")
    state(ctx, EVERYTHING, file_urls={"file:a.mp3": "/canvas/sessions/s/files/T1"}, theme={"--cv-fg": "#123", "--cv-bg": "url(x)"})
    tags = set(js(ctx, "FD.root.all().map(function (n) { return n.tagName; })"))
    assert tags <= ALLOWED_TAGS, sorted(tags - ALLOWED_TAGS)
    assert {"H2", "STRONG", "EM", "CODE", "BR", "SECTION", "BUTTON", "SELECT", "INPUT", "HR", "IMG", "AUDIO", "IFRAME", "PRE"} <= tags
    attributes = set(js(ctx, "[].concat.apply([], FD.root.all().map(function (n) { return Object.keys(n.attrs); }))"))
    assert attributes <= ALLOWED_ATTRIBUTES, sorted(attributes - ALLOWED_ATTRIBUTES)
    texts = js(ctx, "(function () { var out = []; (function walk(n) { n.childNodes.forEach(function (c) { "
                    "if (c.nodeType === 3) out.push(c.data); else walk(c); }); })(FD.root); return out; })()")
    assert texts.count(HOSTILE) >= 7, "the hostile string reaches the DOM only as text"
    assert js(ctx, "FD.find('head', 'strong').textContent") == HOSTILE
    assert js(ctx, "FD.find('head', 'em').textContent") == "x" and js(ctx, "FD.find('head', 'code').textContent") == "y"
    assert js(ctx, "FD.byId('docs').textContent") == HOSTILE + "docs.example"
    assert js(ctx, "FD.find('pic', 'img').alt") == HOSTILE and js(ctx, "FD.find('pic', 'img').src") == "https://img.example/a.png"
    assert js(ctx, "FD.find('pick', 'select').selectedIndex") == 2, "the value b, after the placeholder"
    assert js(ctx, "FD.byId('row').style.props") == {"gap": "4px", "align-items": "flex-end", "flex-wrap": "nowrap"}
    assert js(ctx, "FD.document.documentElement.style.props") == {"--cv-fg": "#123"}, "the theme went through themeCss's rules"
    srcdoc = js(ctx, "FD.find('widget', 'iframe').srcdoc")
    assert "--cv-fg:#123;" in srcdoc and "url(x)" not in srcdoc


def test_a_new_state_moves_removes_and_keeps_nodes(ctx):
    ctx.eval("FD.connect();")
    state(ctx, els(a={"type": "Text", "props": {"text": "A"}}, b={"type": "Text", "props": {"text": "B"}}))
    ctx.eval("var nodeA = FD.byId('a'); nodeA.marker = 1;")
    state(ctx, els(b={"type": "Text", "props": {"text": "B"}}, a={"type": "Text", "props": {"text": "A2"}}))
    assert js(ctx, "FD.root.textContent") == "BA2" and js(ctx, "FD.byId('a').marker") == 1
    state(ctx, els(b={"type": "Text", "props": {"text": "B", "hidden": True}}))
    assert js(ctx, "FD.byId('a')") is None and js(ctx, "FD.byId('b').hidden") is True


def test_a_button_click_sends_one_event_and_shows_busy(ctx):
    ctx.eval("FD.connect();")
    state(ctx, els(go={"type": "Button", "props": {"label": "Go"}}, off={"type": "Button", "props": {"label": "Off", "disabled": True}}))
    ctx.eval("FD.byId('go').click({isTrusted: false});")
    assert events(ctx) == [], "a script's click is not a person's"
    ctx.eval("FD.byId('go').click(); FD.byId('go').click(); FD.byId('off').click();")
    assert events(ctx) == [{"type": "event", "element": "go", "kind": "click"}]
    assert js(ctx, "[FD.byId('go').disabled, FD.byId('go').attrs['aria-busy']]") == [True, "true"]
    ctx.eval("FD.post({type: 'busy', element: 'go', on: false});")
    assert js(ctx, "[FD.byId('go').disabled, FD.byId('go').attrs['aria-busy'] || null]") == [False, None]
    ctx.eval("FD.byId('go').click(); FD.advance(30001);")
    assert js(ctx, "FD.byId('go').disabled") is False, "a click the console never answers is clear after 30 s"


def test_select_changes_send_the_chosen_value(ctx):
    ctx.eval("FD.connect();")
    state(ctx, els(one={"type": "Select", "props": {"label": "One", "options": [["a", "Alpha"], ["b", "Beta"]]}},
                   many={"type": "Select", "props": {"label": "Many", "options": ["x", "y", "z"], "multiple": True}}))
    assert js(ctx, "FD.find('one', 'select').selectedIndex") == 0, "no value: the placeholder"
    ctx.eval("var s = FD.find('one', 'select'); s.selectedIndex = 2; s.dispatch('change');")
    ctx.eval("var boxes = FD.byId('many').query(function (n) { return n.tagName === 'INPUT'; });"
             "boxes[2].checked = true; boxes[2].dispatch('change'); boxes[0].checked = true; boxes[0].dispatch('change');")
    assert events(ctx) == [
        {"type": "event", "element": "one", "kind": "change", "value": "b"},
        {"type": "event", "element": "many", "kind": "change", "value": ["z"]},
        {"type": "event", "element": "many", "kind": "change", "value": ["x", "z"]},
    ]
    state(ctx, els(one={"type": "Select", "props": {"label": "One", "options": [["a", "Alpha"], ["b", "Beta"]]}},
                   many={"type": "Select", "props": {"label": "Many", "options": ["x", "y", "z"], "multiple": True}}))
    assert js(ctx, "FD.find('one', 'select').selectedIndex") == 2, "a refetch that does not hold the change yet keeps the person's choice"


# --- ruling 3: media keyed on the file path --------------------------------------------------------------------------------------------


def test_a_playing_audio_never_restarts_on_a_new_file_url(ctx):
    ctx.eval("FD.connect();")
    song = els(song={"type": "Audio", "props": {"src": "file:music/a.mp3", "title": "A"}})
    state(ctx, song, file_urls={"file:music/a.mp3": "/canvas/sessions/s/files/T1"})
    ctx.eval("var audio = FD.find('song', 'audio'); audio.marker = 'first'; audio.readyState = 4; audio.paused = false; audio.currentTime = 42;")
    assert js(ctx, "[audio.src, audio.writes.src, audio.controls]") == ["/canvas/sessions/s/files/T1", 1, True]
    state(ctx, song, rev=2, file_urls={"file:music/a.mp3": "/canvas/sessions/s/files/T2"})
    assert js(ctx, "FD.find('song', 'audio').marker") == "first", "the same element"
    assert js(ctx, "[audio.src, audio.writes.src, audio.pauses]") == ["/canvas/sessions/s/files/T1", 1, 0], "not reloaded, not paused"
    ctx.eval("audio.dispatch('error');")
    assert js(ctx, "[audio.src, audio.currentTime]") == ["/canvas/sessions/s/files/T2", 42], "an expired URL gives way to the new one"
    state(ctx, els(song={"type": "Audio", "props": {"src": "file:music/b.mp3"}}), rev=3,
          file_urls={"file:music/b.mp3": "/canvas/sessions/s/files/T3"})
    assert js(ctx, "FD.find('song', 'audio').marker || null") is None and js(ctx, "audio.pauses") == 1, "another file is another element"


def test_an_audio_that_loaded_nothing_takes_the_new_url_at_once(ctx):
    ctx.eval("FD.connect();")
    song = els(song={"type": "Audio", "props": {"src": "file:a.mp3"}})
    state(ctx, song, file_urls={"file:a.mp3": "/canvas/sessions/s/files/T1"})
    state(ctx, song, file_urls={"file:a.mp3": "/canvas/sessions/s/files/T2"})
    assert js(ctx, "FD.find('song', 'audio').src") == "/canvas/sessions/s/files/T2"


def test_an_image_is_kept_on_a_new_url_and_swapped_when_it_fails(ctx):
    ctx.eval("FD.connect();")
    pic = els(pic={"type": "Image", "props": {"src": "file:p.png", "alt": "P"}})
    state(ctx, pic, file_urls={"file:p.png": "/canvas/sessions/s/files/T1"})
    ctx.eval("var img = FD.find('pic', 'img');")
    state(ctx, pic, file_urls={"file:p.png": "/canvas/sessions/s/files/T2"})
    assert js(ctx, "[FD.find('pic', 'img') === img, img.src, img.writes.src]") == [True, "/canvas/sessions/s/files/T1", 1]
    ctx.eval("img.dispatch('error');")
    assert js(ctx, "img.src") == "/canvas/sessions/s/files/T2"


def test_audio_events_carry_the_position(ctx):
    ctx.eval("FD.connect();")
    state(ctx, els(song={"type": "Audio", "props": {"src": "https://a.example/x.mp3"}}))
    ctx.eval("var a = FD.find('song', 'audio'); a.currentTime = 12.3456; a.dispatch('play'); a.dispatch('pause'); "
             "a.currentTime = NaN; a.dispatch('ended'); a.dispatch('play', {isTrusted: false});")
    assert events(ctx) == [
        {"type": "event", "element": "song", "kind": "play", "position": 12.346},
        {"type": "event", "element": "song", "kind": "pause", "position": 12.346},
        {"type": "event", "element": "song", "kind": "ended"},
    ]


def test_the_media_policy_draws_its_placeholders(ctx):
    ctx.eval("FD.connect();")
    media = els(a={"type": "Image", "props": {"src": "https://b.example.org/x.png", "alt": "B"}},
                s={"type": "Audio", "props": {"src": "https://b.example.org/x.mp3"}},
                f={"type": "Image", "props": {"src": "file:gone.png", "alt": "G"}})
    state(ctx, media, settings={"media_policy": "allowlist", "media_allowlist": ["*.example.com"], "strict_mode": False})
    assert js(ctx, "FD.byId('a').textContent") == "Load image from b.example.org"
    assert js(ctx, "FD.byId('s').textContent") == "Load audio from b.example.org"
    assert js(ctx, "FD.byId('f').textContent") == "This file is not available"
    ctx.eval("FD.find('a', 'button').click();")
    assert js(ctx, "FD.find('a', 'img').src") == "https://b.example.org/x.png", "a click loads it"
    state(ctx, media, settings={"media_policy": "none", "media_allowlist": [], "strict_mode": False})
    assert js(ctx, "FD.byId('s').textContent") == "Media from b.example.org is blocked by this install's canvas policy"


# --- links -------------------------------------------------------------------------------------------------------------------------


def test_links_open_through_the_console_with_their_host_shown(ctx):
    ctx.eval("FD.connect();")
    state(ctx, els(ok={"type": "Link", "props": {"label": "Docs", "href": "https://docs.example/a?b=1"}},
                   bad={"type": "Link", "props": {"label": "Bad", "href": "javascript:alert(1)"}}))
    assert js(ctx, "FD.byId('ok').textContent") == "Docsdocs.example"
    assert js(ctx, "[FD.byId('bad').disabled, FD.byId('bad').textContent]") == [True, "BadThis link is not valid"]
    ctx.eval("FD.byId('ok').click(); FD.byId('bad').click();")
    assert events(ctx) == [{"type": "open_link", "element": "ok", "url": "https://docs.example/a?b=1"}]


# --- ruling 5: inert Html and Run ---------------------------------------------------------------------------------------------------


def test_html_is_inert_until_run_unless_the_console_says_this_viewer_may_run_it(ctx):
    """The lead's ruling 5 on S7: ``run_html`` (S8 sets it for the session's creator only) decides; absent is inert."""
    ctx.eval("FD.connect();")
    widget = els(w={"type": "Html", "props": {"html": "<button>hi</button>"}})
    state(ctx, widget, run_html=None)
    assert js(ctx, "FD.find('w', 'iframe')") is None
    assert js(ctx, "FD.byId('w').textContent") == "This element runs code the agent wrote.Run"
    ctx.eval("var run = FD.byId('w').query(function (n) { return n.attrs['data-cv'] === 'html-run'; })[0];")
    assert js(ctx, "run.tagName") == "BUTTON"
    ctx.eval("run.click({isTrusted: false});")
    assert js(ctx, "FD.find('w', 'iframe')") is None, "a script cannot press Run"
    ctx.eval("run.click();")
    assert js(ctx, "FD.find('w', 'iframe').attrs.sandbox") == "allow-scripts allow-forms"
    state(ctx, widget, rev=2, run_html=False)
    assert js(ctx, "FD.find('w', 'iframe').writes.srcdoc") == 1, "the click holds for that body for the page load"
    state(ctx, els(w={"type": "Html", "props": {"html": "<form>log in</form>"}}), rev=3, run_html=False)
    assert js(ctx, "FD.find('w', 'iframe')") is None, "a changed body asks again"


def test_html_runs_at_once_for_the_creator_and_strict_mode_holds_it_for_everyone(ctx):
    ctx.eval("FD.connect();")
    widget = els(w={"type": "Html", "props": {"html": "<p>x</p>"}})
    state(ctx, widget, run_html=True)
    assert js(ctx, "FD.find('w', 'iframe') !== null") is True
    state(ctx, widget, rev=2, run_html=True, settings={"media_policy": "any-https", "media_allowlist": [], "strict_mode": True})
    assert js(ctx, "FD.find('w', 'iframe')") is None and "Run" in js(ctx, "FD.byId('w').textContent")


def test_html_frames_are_keyed_on_their_content_not_the_rev(ctx):
    ctx.eval("FD.connect();")
    state(ctx, els(w={"type": "Html", "props": {"html": "<p>x</p>"}}, t={"type": "Text", "props": {"text": "a"}}))
    ctx.eval("var frame = FD.find('w', 'iframe');")
    state(ctx, els(w={"type": "Html", "props": {"html": "<p>x</p>"}}, t={"type": "Text", "props": {"text": "b"}}), rev=2)
    assert js(ctx, "[FD.find('w', 'iframe') === frame, frame.writes.srcdoc]") == [True, 1]
    state(ctx, els(w={"type": "Html", "props": {"html": "<p>y</p>"}}), rev=3)
    assert js(ctx, "FD.find('w', 'iframe') === frame") is False


# --- ruling 1: the click gate lives in the renderer ----------------------------------------------------------------------------------


def _html_ready(ctx, *ids: str):
    ctx.eval("FD.connect();")
    state(ctx, els(**{i: {"type": "Html", "props": {"html": "<button>x</button>"}} for i in ids}), values={ids[0]: {"n": 1}})
    for i in ids:
        ctx.eval(f"var port_{i} = FD.load('{i}'); var frame_{i} = FD.find('{i}', 'iframe');")


def test_each_load_offers_the_frame_its_own_port(ctx):
    _html_ready(ctx, "w")
    offer = js(ctx, "(function (p) { return {data: p.data, origin: p.origin, ports: p.transfer.length}; })(frame_w.contentWindow.posted[0])")
    assert offer == {"data": {"type": "primer.html.connect", "element": "w"}, "origin": "*", "ports": 1}
    assert js(ctx, "port_w.other.sent") == [{"type": "value", "value": {"n": 1}}], "the element's value follows the offer"


def test_html_emits_pass_only_on_the_renderers_activation(ctx):
    _html_ready(ctx, "w")
    emit = "port_w.postMessage({type: 'emit', name: 'clicked', data: {n: 1}});"
    ctx.eval(emit)
    assert events(ctx) == [], "no activation"
    ctx.eval("FD.window.navigator.userActivation.isActive = true;" + emit)
    assert events(ctx) == [], "activation, but the frame is not the focused element"
    ctx.eval("frame_w.focus();" + emit * 5)
    assert events(ctx) == [{"type": "event", "element": "w", "kind": "emit", "name": "clicked", "data": {"n": 1}}] * 3, "3 per activation"
    ctx.eval("FD.advance(1000);" + emit)
    assert len(events(ctx)) == 3, "the same activation, still spent"
    ctx.eval("FD.window.navigator.userActivation.isActive = false; FD.advance(150);"
             "FD.window.navigator.userActivation.isActive = true;" + emit * 4)
    assert len(events(ctx)) == 6, "a new activation, a new budget"
    ctx.eval("FD.window.navigator.userActivation.isActive = false; FD.advance(150); FD.window.navigator.userActivation.isActive = true;"
             "FD.document.visibilityState = 'hidden';" + emit)
    assert len(events(ctx)) == 6, "not while the page is hidden"


def test_a_looping_element_cannot_emit_after_its_activation_expires(ctx):
    _html_ready(ctx, "w")
    ctx.eval("frame_w.focus(); FD.window.navigator.userActivation.isActive = true;")
    for _ in range(20):
        ctx.eval("port_w.postMessage({type: 'emit', name: 'loop'}); FD.advance(500);")
        if js(ctx, "FD.clock.t") > 1000000 + 5000:
            ctx.eval("FD.window.navigator.userActivation.isActive = false;")
    assert len(events(ctx)) == 3, "one click, at most three emits, however long the loop runs"
    assert js(ctx, "FD.activeTimers()") == 0, "the activation poll stops once the activation is over"


def test_copy_and_open_link_share_the_gate_and_carry_no_other_element(ctx):
    _html_ready(ctx, "w")
    ctx.eval("port_w.postMessage({type: 'copy', text: 'secret'}); port_w.postMessage({type: 'open_link', url: 'https://a.example/'});")
    assert events(ctx) == []
    ctx.eval("frame_w.focus(); FD.window.navigator.userActivation.isActive = true;"
             "port_w.postMessage({type: 'copy', text: 'hello'}); port_w.postMessage({type: 'open_link', url: 'https://a.example/'});"
             "port_w.postMessage({type: 'open_link', url: 'javascript:alert(1)'});")
    assert events(ctx) == [{"type": "copy", "element": "w", "text": "hello"}, {"type": "open_link", "url": "https://a.example/"}]


def test_an_element_speaks_only_for_itself(ctx):
    _html_ready(ctx, "a", "b")
    ctx.eval("frame_a.focus(); FD.window.navigator.userActivation.isActive = true;")
    ctx.eval("port_b.postMessage({type: 'emit', name: 'from_b'});")
    ctx.eval("port_a.postMessage({type: 'emit', name: 'as_b', element: 'b'});")
    ctx.eval("port_a.postMessage({type: 'event', element: 'b', kind: 'emit', name: 'x'});")
    assert events(ctx) == [], "b is not focused; a cannot name b, nor send a console message"
    ctx.eval("port_a.postMessage({type: 'emit', name: 'mine'});")
    assert events(ctx) == [{"type": "event", "element": "a", "kind": "emit", "name": "mine", "data": None}]


def test_a_reloaded_frame_gets_a_new_port_and_the_old_one_is_not_heard(ctx):
    _html_ready(ctx, "w")
    ctx.eval("var old = port_w; var fresh = FD.load('w'); frame_w.focus(); FD.window.navigator.userActivation.isActive = true;"
             "old.postMessage({type: 'emit', name: 'stale'}); fresh.postMessage({type: 'emit', name: 'current'});")
    assert [e["name"] for e in events(ctx)] == ["current"]
    assert js(ctx, "old.other.closed") is True


def test_set_value_needs_no_click_and_goes_out_once_a_second(ctx):
    _html_ready(ctx, "w")
    ctx.eval("for (var i = 0; i < 30; i++) port_w.postMessage({type: 'set_value', value: i}); FD.advance(0);")
    assert events(ctx) == [{"type": "event", "element": "w", "kind": "set_value", "value": 29}], "a burst goes out once, its latest value"
    ctx.eval("port_w.postMessage({type: 'set_value', value: 30}); port_w.postMessage({type: 'set_value', value: 31}); FD.advance(999);")
    assert len(events(ctx)) == 1, "not twice within a second"
    ctx.eval("FD.advance(1);")
    assert events(ctx)[-1] == {"type": "event", "element": "w", "kind": "set_value", "value": 31}
    ctx.eval("FD.advance(5000); port_w.postMessage({type: 'set_value', value: 31}); FD.advance(5000);")
    assert len(events(ctx)) == 2, "a value the canvas already holds is not sent again"


def test_the_frame_is_sized_within_bounds(ctx):
    _html_ready(ctx, "w")
    assert js(ctx, "frame_w.style.props.height") == "150px"
    ctx.eval("port_w.postMessage({type: 'size', height: 5000});")
    assert js(ctx, "frame_w.style.props.height") == "1200px"
    ctx.eval("port_w.postMessage({type: 'size', height: 3});")
    assert js(ctx, "frame_w.style.props.height") == "40px"


def test_a_disabled_or_hidden_html_element_is_not_heard(ctx):
    ctx.eval("FD.connect();")
    state(ctx, els(w={"type": "Html", "props": {"html": "<p>x</p>", "disabled": True}}))
    ctx.eval("var p = FD.load('w'); FD.find('w', 'iframe').focus(); FD.window.navigator.userActivation.isActive = true;"
             "p.postMessage({type: 'emit', name: 'x'}); p.postMessage({type: 'set_value', value: 1}); FD.advance(2000);")
    assert events(ctx) == [] and js(ctx, "FD.byId('w').attrs.inert") == ""


def test_the_value_and_theme_reach_the_html_frame(ctx):
    _html_ready(ctx, "w")
    ctx.eval("port_w.received = []; var mine = port_w;")
    state(ctx, els(w={"type": "Html", "props": {"html": "<button>x</button>"}}), rev=2, values={"w": {"n": 2}})
    state(ctx, els(w={"type": "Html", "props": {"html": "<button>x</button>"}}), rev=3, values={"w": {"n": 2}})
    ctx.eval("FD.post({type: 'theme', theme: {'--cv-fg': '#222', '--cv-bg': 'url(x)'}});")
    assert js(ctx, "mine.other.sent.slice(1)") == [
        {"type": "value", "value": {"n": 2}},
        {"type": "theme", "theme": {"--cv-fg": "#222"}},
    ]


def test_removed_elements_are_torn_down(ctx):
    ctx.eval("FD.connect();")
    state(ctx, els(w={"type": "Html", "props": {"html": "<p>x</p>"}}, s={"type": "Audio", "props": {"src": "https://a.example/a.mp3"}}))
    ctx.eval("var p = FD.load('w'); var audio = FD.find('s', 'audio');")
    state(ctx, els(), rev=2)
    assert js(ctx, "[p.other.closed, audio.pauses, FD.root.textContent]") == [True, 1, ""]


def test_outgoing_messages_are_capped_at_twenty_a_second(ctx):
    ctx.eval("FD.connect();")
    buttons = {f"b{i}": {"type": "Button", "props": {"label": str(i)}} for i in range(30)}
    state(ctx, els(**buttons))
    ctx.eval("for (var i = 0; i < 30; i++) FD.byId('b' + i).click();")
    assert len(events(ctx)) == 19, "20 a second, the ready message among them"
    ctx.eval("FD.advance(1000); FD.post({type: 'busy', element: 'b29', on: false}); FD.byId('b29').click();")
    assert len(events(ctx)) == 20


def test_an_element_that_throws_in_a_loop_cannot_crowd_out_a_click(ctx):
    _html_ready(ctx, "w")
    state(ctx, els(w={"type": "Html", "props": {"html": "<button>x</button>"}}, go={"type": "Button", "props": {"label": "Go"}}))
    ctx.eval("for (var i = 0; i < 100; i++) port_w.postMessage({type: 'error', message: 'boom ' + i});")
    errors = [m for m in js(ctx, "FD.sent()") if m["type"] == "error"]
    assert errors == [{"type": "error", "message": f"w: boom {i}"} for i in range(3)]
    ctx.eval("FD.byId('go').click();")
    assert events(ctx)[-1] == {"type": "event", "element": "go", "kind": "click"}

