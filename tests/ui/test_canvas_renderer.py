"""The canvas frame's trusted renderer, its pure part (pivot P5, Task 16; design 8.6, plan Decisions 6 and 10).

Every assertion runs ``ui/canvas/renderer.js`` itself in MiniRacer (``var window = globalThis;`` then the file): the media policy, the
inline markup, links, the theme, Select options, the console's messages and the outgoing bucket. The DOM side (``CV.boot``) is
``tests/ui/test_canvas_renderer_dom.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"
RENDERER = UI / "canvas" / "renderer.js"

ANY = {"media_policy": "any-https", "media_allowlist": [], "strict_mode": False}
STATE = {
    "type": "state",
    "rev": 3,
    "elements": {
        "root": {"type": "Column", "props": {}, "children": ["title", "play"]},
        "title": {"type": "Text", "props": {"text": "Playlist", "style": "h2"}},
        "play": {"type": "Button", "props": {"label": "Play", "style": "primary"}},
    },
    "values": {},
    "settings": ANY,
    "file_urls": {},
    "own_origin": "",
    "theme": {},
}


@pytest.fixture
def ctx():
    py_mini_racer = pytest.importorskip("py_mini_racer")
    context = py_mini_racer.MiniRacer()
    context.eval("var window = globalThis;")
    context.eval(RENDERER.read_text(encoding="utf-8"))
    yield context
    context.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def call(ctx, fn: str, *args):
    return js(ctx, f"CV.{fn}(" + ", ".join(json.dumps(a) for a in args) + ")")


def test_the_file_publishes_the_renderer_on_window(ctx):
    assert js(ctx, "typeof window.CV.boot") == "function"
    assert js(ctx, "CV.PROTOCOL") == 1


# --- the media policy ---------------------------------------------------------------------------------------------------------------


def test_media_decisions_follow_the_policy(ctx):
    assert js(ctx, 'CV.mediaDecision("https://a.example/x.png", {media_policy: "any-https", media_allowlist: []}, {}, "")') == {
        "kind": "load", "url": "https://a.example/x.png",
    }
    assert js(ctx, 'CV.mediaDecision("https://cdn.example.com/x.png", {media_policy: "allowlist", media_allowlist: ["*.example.com"]}, {}, "")')["kind"] == "load"
    assert js(ctx, 'CV.mediaDecision("https://b.example.org/x.png", {media_policy: "allowlist", media_allowlist: ["*.example.com"]}, {}, "")') == {
        "kind": "click", "host": "b.example.org",
    }
    assert js(ctx, 'CV.mediaDecision("https://a.example/x.png", {media_policy: "none", media_allowlist: []}, {}, "")') == {
        "kind": "blocked", "host": "a.example",
    }
    assert js(ctx, 'CV.mediaDecision("file:c.png", {media_policy: "none", media_allowlist: []}, {"file:c.png": "/canvas/sessions/s/files/t"}, "")')["kind"] == "load"
    assert js(ctx, 'CV.mediaDecision("file:d.png", {media_policy: "none", media_allowlist: []}, {}, "")') == {"kind": "missing"}


def test_the_allowlist_matches_a_host_or_a_wildcard_suffix_only(ctx):
    allow = {"media_policy": "allowlist", "media_allowlist": ["media.example.net", "*.example.com"]}
    assert call(ctx, "mediaDecision", "https://media.example.net/a.png", allow, {}, "")["kind"] == "load"
    assert call(ctx, "mediaDecision", "https://a.b.example.com/a.png", allow, {}, "")["kind"] == "load"
    assert call(ctx, "mediaDecision", "https://example.com/a.png", allow, {}, "") == {"kind": "click", "host": "example.com"}
    assert call(ctx, "mediaDecision", "https://evilexample.com/a.png", allow, {}, "")["kind"] == "click"
    assert call(ctx, "mediaDecision", "https://media.example.net.evil.org/a.png", allow, {}, "")["kind"] == "click"
    assert call(ctx, "mediaDecision", "https://x.media.example.net/a.png", allow, {}, "")["kind"] == "click"


def test_files_load_their_minted_url_under_every_policy_and_data_loads(ctx):
    urls = {"file:assets/c.png": "/canvas/sessions/ses-1/files/abc.def-_"}
    for policy in ("any-https", "allowlist", "none"):
        settings = {"media_policy": policy, "media_allowlist": []}
        assert call(ctx, "mediaDecision", "file:assets/c.png", settings, urls, "") == {
            "kind": "load", "url": "/canvas/sessions/ses-1/files/abc.def-_",
        }
        assert call(ctx, "mediaDecision", "data:image/png;base64,iVBORw0KGgo=", settings, {}, "")["kind"] == "load"
    assert call(ctx, "mediaDecision", "FILE:assets/c.png", ANY, urls, "")["kind"] == "load", "the scheme in any case, as the server matches it"


@pytest.mark.parametrize("url", [
    "https://evil.example/files/x", "//evil.example/canvas/sessions/s/files/t", "/canvas/sessions/s/files/t?x=1",
    "/v1/workspaces/w", "/canvas/sessions/s/files/t\"onerror", "/canvas/sessions/../files/t/x",
])
def test_a_file_url_that_is_not_a_minted_path_is_missing(ctx, url):
    assert call(ctx, "mediaDecision", "file:c.png", ANY, {"file:c.png": url}, "") == {"kind": "missing"}


@pytest.mark.parametrize("src", [
    "http://a.example/x.png", "ftp://a.example/x.png", "https://user:pw@a.example/x.png", "https://a.example/x y.png",
    "https://a.example\\x.png", "data:text/plain;base64,AAAA", "", "https://", "javascript:alert(1)",
])
def test_a_source_the_renderer_cannot_load_is_missing(ctx, src):
    assert call(ctx, "mediaDecision", src, ANY, {}, "") == {"kind": "missing"}


def test_media_is_keyed_on_the_file_path_not_the_url(ctx):
    """The lead's ruling after S6: the key of a file is ``file:<path>`` (URLs are re-minted every window)."""
    assert call(ctx, "mediaKey", "file:a/b.mp3") == "file:a/b.mp3"
    assert call(ctx, "mediaKey", "FILE:a/b.mp3") == "file:a/b.mp3"
    assert call(ctx, "mediaKey", "https://x.example/a.mp3") == "https://x.example/a.mp3"


# --- text, links, theme and options ----------------------------------------------------------------------------------------------------


def test_inline_markup(ctx):
    assert js(ctx, 'CV.inlineMarkup("a **b** _c_ `d`")') == [["", "a "], ["b", "b"], ["", " "], ["i", "c"], ["", " "], ["code", "d"]]


@pytest.mark.parametrize("text, expected", [
    ("plain", [["", "plain"]]),
    ("snake_case_name", [["", "snake_case_name"]]),
    ("**unclosed and _half", [["", "**unclosed and _half"]]),
    ("** spaced **", [["", "** spaced **"]]),
    ("``", [["", "``"]]),
    ("line one\nline **two**", [["", "line one\nline "], ["b", "two"]]),
    ("**across\nlines**", [["", "**across\nlines**"]]),
    ("`a **b** c`", [["code", "a **b** c"]]),
    ("<img src=x onerror=alert(1)>", [["", "<img src=x onerror=alert(1)>"]]),
    ("", []),
])
def test_inline_markup_leaves_what_is_not_a_span_literal(ctx, text, expected):
    assert call(ctx, "inlineMarkup", text) == expected


def test_links_are_http_or_https_only(ctx):
    assert call(ctx, "linkTarget", "https://docs.example.com/a?b=1#c") == {"url": "https://docs.example.com/a?b=1#c", "host": "docs.example.com"}
    assert call(ctx, "linkTarget", "http://LOCALHOST:9000/x") == {"url": "http://LOCALHOST:9000/x", "host": "localhost:9000"}
    for bad in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<b>x</b>", "ftp://a.example/", "https://u:p@a.example/",
                "https://a.example/x y", "//a.example/", "https://a.example:99999/", "mailto:a@b.example", "https://a.example\\@b.example/"):
        assert call(ctx, "linkTarget", bad) is None, bad


def test_theme_css_drops_unsafe_values(ctx):
    theme = {
        "--cv-bg": "red;}", "--cv-fg": "url(x)", "--other": "blue", "--cv-accent": "#0969da",
        "--cv-a": "U\\72L(x)", "--cv-b": "image-set(\"https://t.example/x.png\" 1x)", "--cv-c": "a /* b", "--cv-d": "</style><script>",
        "--cv-e": "color-mix(in srgb, #fff 20%, rgb(1 2 3))", "--cv-font": "system-ui, \"Segoe UI\", sans-serif", "--cv-g": "\"unbalanced",
    }
    assert call(ctx, "themeCss", theme) == (
        ":root{--cv-accent:#0969da;--cv-e:color-mix(in srgb, #fff 20%, rgb(1 2 3));--cv-font:system-ui, \"Segoe UI\", sans-serif;}"
    )
    assert call(ctx, "themeCss", {}) == "" and call(ctx, "themeCss", None) == ""


def test_select_options(ctx):
    assert call(ctx, "selectOptions", ["A", "B"]) == [{"value": "A", "label": "A"}, {"value": "B", "label": "B"}]
    assert call(ctx, "selectOptions", [["a", "Alpha"], ["b", "Beta"]]) == [{"value": "a", "label": "Alpha"}, {"value": "b", "label": "Beta"}]
    assert call(ctx, "selectOptions", ["A", "A", 3, ["x"], ["y", 2], "B"]) == [{"value": "A", "label": "A"}, {"value": "B", "label": "B"}]
    assert len(call(ctx, "selectOptions", [str(i) for i in range(150)])) == 100
    assert call(ctx, "selectOptions", "A,B") == []


# --- the console's messages ------------------------------------------------------------------------------------------------------------


def test_host_messages_are_validated(ctx):
    state = call(ctx, "validateHostMessage", STATE)
    assert state["type"] == "state" and state["rev"] == 3
    assert state["elements"]["play"] == {"type": "Button", "props": {"hidden": False, "disabled": False, "label": "Play", "style": "primary"}, "children": []}
    assert call(ctx, "validateHostMessage", {"type": "busy", "element": "play", "on": True}) == {"type": "busy", "element": "play", "on": True}
    assert call(ctx, "validateHostMessage", {"type": "theme", "theme": {"--cv-bg": "#fff", "--cv-x": "url(y)"}}) == {
        "type": "theme", "theme": {"--cv-bg": "#fff"},
    }
    for bad in (
        None, "state", [STATE], {"type": "nope"}, {"type": "state"},
        {**STATE, "rev": -1}, {**STATE, "rev": 1.5}, {**STATE, "rev": "3"},
        {**STATE, "elements": []}, {**STATE, "values": None}, {**STATE, "settings": {"media_policy": "some", "strict_mode": False}},
        {**STATE, "settings": {"media_policy": "none", "media_allowlist": []}}, {**STATE, "file_urls": "x"},
        {**STATE, "elements": {"play": STATE["elements"]["play"]}},
        {**STATE, "elements": {**STATE["elements"], "root": {"type": "Row", "props": {}, "children": []}}},
        {"type": "busy", "element": "Play", "on": True}, {"type": "busy", "element": "play", "on": 1},
        {"type": "theme", "theme": "x"},
    ):
        assert call(ctx, "validateHostMessage", bad) is None, bad


def test_an_oversized_state_is_dropped(ctx):
    big = {**STATE, "elements": {**STATE["elements"], "t": {"type": "Text", "props": {"text": "x" * 10000}}}}
    assert call(ctx, "validateHostMessage", big) is not None
    huge = {**STATE, "values": {f"v{i}": "x" * 1000 for i in range(2200)}}
    assert len(json.dumps(huge)) > 2 * 1024 * 1024
    assert call(ctx, "validateHostMessage", huge) is None


def test_a_state_that_is_not_json_is_dropped(ctx):
    """A structured clone can carry a Date, a Map, a cycle or a shared node: none of them is canvas state."""
    ctx.eval("var S = " + json.dumps(STATE) + ";")
    for value in ("new Date(0)", "new Map()", "NaN", "undefined", "function () {}"):
        assert js(ctx, f"CV.validateHostMessage(Object.assign({{}}, S, {{values: {{a: {value}}}}}))") is None, value
    cyclic = js(ctx, "(function () { var v = {}; v.self = v; return CV.validateHostMessage(Object.assign({}, S, {values: {a: v}})); })()")
    assert cyclic is None
    bomb = js(ctx, '(function () { var x = "x"; for (var i = 0; i < 30; i++) x = [x, x]; '
                   "return CV.validateHostMessage(Object.assign({}, S, {values: {a: x}})); })()")
    assert bomb is None, "a shared node walked 2^30 times is refused by its node budget, not walked"


def test_the_state_keeps_only_what_the_renderer_reads(ctx):
    msg = {
        **STATE,
        "elements": {
            **STATE["elements"],
            "bad id": {"type": "Text", "props": {"text": "x"}},
            "unknown": {"type": "Marquee", "props": {}},
            "pic": {"type": "Image", "props": {"src": "file:a.png", "alt": "A", "onerror": "x", "height": 99999}},
        },
        "values": {"pic": 1, "Bad": 2},
        "file_urls": {"file:a.png": "/canvas/sessions/s/files/t", "file:b.png": "https://evil.example/x", "a.png": "/canvas/sessions/s/files/t"},
        "own_origin": "http://h:9000; script-src *",
        "settings": {"media_policy": "allowlist", "media_allowlist": ["A.example.com", "bad host", "*.ok.example"], "strict_mode": True},
    }
    out = call(ctx, "validateHostMessage", msg)
    assert set(out["elements"]) == {"root", "title", "play", "pic"}
    assert out["elements"]["pic"]["props"] == {"hidden": False, "disabled": False, "src": "file:a.png", "alt": "A", "fit": "contain", "height": None}
    assert out["values"] == {"pic": 1}
    assert out["file_urls"] == {"file:a.png": "/canvas/sessions/s/files/t"}
    assert out["own_origin"] == ""
    assert out["settings"] == {"media_policy": "allowlist", "media_allowlist": ["a.example.com", "*.ok.example"], "strict_mode": True}


def test_only_the_parents_handshake_is_accepted(ctx):
    """The lead's ruling 4 on S7, the handshake: from window.parent (the console), the exact shape, exactly one port."""
    ctx.eval("var P = {name: 'console'}, SELF = {name: 'frame'}, OTHER = {name: 'html'}, PORT = {};")
    ok = '{type: "primer.canvas.connect", protocol: 1}'
    assert js(ctx, f"CV.acceptsHandshake({ok}, P, P, SELF, [PORT])") is True
    assert js(ctx, f"CV.acceptsHandshake({ok}, OTHER, P, SELF, [PORT])") is False, "an Html frame's parent.postMessage"
    assert js(ctx, f"CV.acceptsHandshake({ok}, null, P, SELF, [PORT])") is False
    assert js(ctx, f"CV.acceptsHandshake({ok}, SELF, SELF, SELF, [PORT])") is False, "an unframed page has no console"
    assert js(ctx, f"CV.acceptsHandshake({ok}, P, P, SELF, [])") is False
    assert js(ctx, f"CV.acceptsHandshake({ok}, P, P, SELF, [PORT, PORT])") is False
    assert js(ctx, 'CV.acceptsHandshake({type: "primer.canvas.connect", protocol: 2}, P, P, SELF, [PORT])') is False
    assert js(ctx, 'CV.acceptsHandshake({type: "primer.html.connect", protocol: 1}, P, P, SELF, [PORT])') is False


# --- the outgoing bucket -----------------------------------------------------------------------------------------------------------


def test_the_bucket_lets_twenty_a_second_through(ctx):
    ctx.eval("var t = 0; var b = CV.makeBucket(20, 20, function () { return t; });")
    assert js(ctx, "(function () { var n = 0; for (var i = 0; i < 50; i++) if (b.take()) n++; return n; })()") == 20
    ctx.eval("t = 500;")
    assert js(ctx, "(function () { var n = 0; for (var i = 0; i < 50; i++) if (b.take()) n++; return n; })()") == 10

