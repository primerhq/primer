"""The element catalog: the table, the signatures and per-argument validation."""

from __future__ import annotations

import pytest

from primer.canvas.catalog import (
    AUDIO_FILE_TYPES,
    CATALOG,
    COMMON_PROPS,
    CONTAINER_TYPES,
    EVENT_KINDS,
    IMAGE_FILE_TYPES,
    ElementSpec,
    Param,
    param_for,
    signature,
    validate_arg,
)
from primer.canvas.lang import Ref

USE_HTTPS = "use https (or file:<path> for a workspace file)"
FILE_REL = "file: paths are relative to the workspace and stay inside it"
IMAGE_EXT = "file:<path> must name a png, jpg, jpeg, gif, webp or avif image"
AUDIO_EXT = "file:<path> must name an mp3, ogg, oga, wav, m4a, aac, flac or weba audio file"
HREF = "Link href must be an http or https URL"


def _check(type_name: str, param_name: str, value, *, max_html_bytes: int = 65536):
    spec = CATALOG[type_name]
    return validate_arg(type_name, param_for(spec, param_name), value, max_html_bytes=max_html_bytes)


def test_the_catalog_lists_the_eleven_types_in_the_documented_order():
    assert list(CATALOG) == ["Column", "Row", "Card", "Divider", "Text", "Button", "Select", "Image", "Audio", "Link", "Html"]
    assert all(isinstance(spec, ElementSpec) and spec.type == name for name, spec in CATALOG.items())
    assert CONTAINER_TYPES == frozenset({"Column", "Row", "Card"})
    assert {name for name, spec in CATALOG.items() if spec.container} == set(CONTAINER_TYPES)
    assert COMMON_PROPS == ("hidden", "disabled")


def test_signatures_follow_the_element_table():
    assert signature("Image") == "Image(src, alt, fit?, height?)"
    assert {name: signature(name) for name in CATALOG} == {
        "Column": "Column(children, gap?, align?)",
        "Row": "Row(children, gap?, align?, wrap?)",
        "Card": "Card(children, title?)",
        "Divider": "Divider()",
        "Text": "Text(text, style?)",
        "Button": "Button(label, style?, disabled?, wake?)",
        "Select": "Select(label, options, value?, multiple?, wake?)",
        "Image": "Image(src, alt, fit?, height?)",
        "Audio": "Audio(src, title?, loop?)",
        "Link": "Link(label, href)",
        "Html": "Html(html, height?)",
    }


def test_event_kinds_and_default_wakes():
    assert EVENT_KINDS["Button"] == {"click": True} and EVENT_KINDS["Select"] == {"change": False}
    assert EVENT_KINDS == {
        "Button": {"click": True},
        "Select": {"change": False},
        "Audio": {"play": False, "pause": False, "ended": False},
        "Link": {"open": False},
        "Html": {"emit": True, "set_value": False},
    }


def test_file_types():
    assert IMAGE_FILE_TYPES == ("png", "jpg", "jpeg", "gif", "webp", "avif")
    assert AUDIO_FILE_TYPES == ("mp3", "ogg", "oga", "wav", "m4a", "aac", "flac", "weba")


def test_the_params_carry_the_documented_bounds_and_defaults():
    p = {name: {q.name: q for q in spec.params} for name, spec in CATALOG.items()}
    assert p["Column"]["gap"] == Param("gap", False, "number", lo=0, hi=64, default=8)
    assert p["Column"]["align"].enum == ("start", "center", "end", "stretch") and p["Column"]["align"].default == "stretch"
    assert p["Row"]["align"].default == "center" and p["Row"]["wrap"].default is True
    assert p["Text"]["style"].enum == ("h1", "h2", "h3", "body", "caption", "code") and p["Text"]["style"].default == "body"
    assert p["Button"]["style"].enum == ("primary", "secondary", "danger") and p["Button"]["style"].default == "secondary"
    assert (p["Button"]["wake"].default, p["Select"]["wake"].default) == (True, False)
    assert (p["Image"]["height"].lo, p["Image"]["height"].hi, p["Html"]["height"].lo, p["Html"]["height"].hi) == (16, 2000, 20, 2000)
    assert [q.name for q in CATALOG["Divider"].params] == []
    assert param_for(CATALOG["Text"], "hidden").kind == "bool" and param_for(CATALOG["Text"], "colour") is None
    assert param_for(CATALOG["Button"], "disabled") is p["Button"]["disabled"]


# per-argument validation


def test_children_must_be_a_list_of_refs():
    assert _check("Column", "children", [Ref("a"), Ref("b")]) is None
    assert _check("Column", "children", []) is None
    assert _check("Column", "children", "a") == "Column needs children as a list"
    assert _check("Column", "children", ["a"]) == "children must be element ids written without quotes, for example [a, b]"
    assert _check("Column", "children", [Ref("Bad")]) == "'Bad' is not an element id (a lowercase letter, then lowercase letters, digits or _, at most 32)"


@pytest.mark.parametrize("type_name, name, value, ok", [
    ("Button", "label", "Go", True), ("Button", "label", "", False), ("Button", "label", "x" * 80, True), ("Button", "label", "x" * 81, False),
    ("Button", "label", 5, False), ("Card", "title", "x" * 200, True), ("Card", "title", "x" * 201, False),
    ("Image", "alt", "x" * 300, True), ("Image", "alt", "x" * 301, False), ("Audio", "title", "x" * 201, False),
])
def test_string_lengths(type_name, name, value, ok):
    got = _check(type_name, name, value)
    assert (got is None) is ok
    if not ok and isinstance(value, str):
        assert got.startswith(f"{name} must be ") and "characters" in got
    if not ok and not isinstance(value, str):
        assert got == f"{name} must be text"


def test_text_may_be_empty_and_has_a_ceiling():
    assert _check("Text", "text", "") is None
    assert _check("Text", "text", "x" * 10000) is None
    assert _check("Text", "text", "x" * 10001) == "text is 10001 characters; the limit is 10000"
    assert _check("Text", "text", 5) == "text must be text"


@pytest.mark.parametrize("type_name, name, value, ok", [
    ("Column", "gap", 0, True), ("Column", "gap", 64, True), ("Column", "gap", 12.5, True), ("Column", "gap", 65, False),
    ("Column", "gap", -1, False), ("Column", "gap", True, False), ("Column", "gap", "8", False), ("Column", "gap", float("nan"), False),
    ("Column", "gap", float("inf"), False), ("Image", "height", 15, False), ("Image", "height", 16, True), ("Html", "height", 19, False),
    ("Html", "height", 2000, True), ("Html", "height", 2001, False),
    # an integer beyond the range of a float is out of range like any other, never an OverflowError
    pytest.param("Column", "gap", 10**400, False, id="gap-huge"),
    pytest.param("Column", "gap", -(10**400), False, id="gap-huge-negative"),
    pytest.param("Image", "height", 10**400, False, id="image-height-huge"),
    pytest.param("Html", "height", 10**400, False, id="html-height-huge"),
    pytest.param("Html", "height", -(10**400), False, id="html-height-huge-negative"),
])
def test_numbers_are_ranged_and_never_booleans(type_name, name, value, ok):
    got = _check(type_name, name, value)
    assert (got is None) is ok
    if not ok:
        assert got.startswith(f"{name} must be a number from ")


def test_number_messages_print_whole_bounds_without_a_decimal():
    assert _check("Column", "gap", 99) == "gap must be a number from 0 to 64"
    assert _check("Html", "height", 1) == "height must be a number from 20 to 2000"
    assert _check("Column", "gap", 10**400) == "gap must be a number from 0 to 64"
    assert _check("Image", "height", -(10**400)) == "height must be a number from 16 to 2000"


def test_booleans_and_enums():
    assert _check("Button", "disabled", True) is None and _check("Button", "disabled", 1) == "disabled must be true or false"
    assert _check("Text", "style", "h1") is None
    assert _check("Text", "style", "h4") == "style must be one of h1, h2, h3, body, caption, code"
    assert _check("Button", "style", "loud") == "style must be one of primary, secondary, danger"
    assert _check("Image", "fit", 3) == "fit must be one of contain, cover"


def test_options():
    assert _check("Select", "options", ["Jazz", "Lo-fi"]) is None
    assert _check("Select", "options", [["a", "A"], ["b", "B"]]) is None
    assert _check("Select", "options", "Jazz") == "Select needs options as a list"
    assert _check("Select", "options", []) == "options must have 1 to 100 entries"
    assert _check("Select", "options", [str(i) for i in range(100)]) is None
    assert _check("Select", "options", [str(i) for i in range(101)]) == "options must have 1 to 100 entries"
    assert _check("Select", "options", ["a", ["b", "B"]]) == "options must be all strings or all [value, label] pairs"
    assert _check("Select", "options", [1, 2]) == "each option must be a string or a [value, label] pair of strings"
    assert _check("Select", "options", [["a"]]) == "each option must be a string or a [value, label] pair of strings"
    assert _check("Select", "options", [["a", 1]]) == "each option must be a string or a [value, label] pair of strings"
    assert _check("Select", "options", ["a", "a"]) == "option values must be unique; 'a' appears twice"
    assert _check("Select", "options", [["a", "A"], ["a", "B"]]) == "option values must be unique; 'a' appears twice"
    assert _check("Select", "options", ["x" * 200]) is None
    assert _check("Select", "options", ["x" * 201]) == "option values are limited to 200 characters"


def test_a_select_value_is_a_string_or_a_list_of_strings():
    assert _check("Select", "value", "Jazz") is None and _check("Select", "value", ["Jazz"]) is None
    assert _check("Select", "value", 3) == "value must be one of the options (a list of them when multiple is true)"
    assert _check("Select", "value", [1]) == "value must be one of the options (a list of them when multiple is true)"


@pytest.mark.parametrize("src, expected", [
    ("https://cdn.example.com/covers/a.jpg", None),
    ("HTTPS://CDN.Example.com/a.jpg", None),
    ("https://cdn.example.com:8443/a.jpg?x=1#y", None),
    ("http://cdn.example.com/a.jpg", USE_HTTPS),
    ("javascript:alert(1)", USE_HTTPS),
    ("//cdn.example.com/a.jpg", USE_HTTPS),
    ("ftp://cdn.example.com/a.jpg", USE_HTTPS),
    ("cdn.example.com/a.jpg", USE_HTTPS),
    ("https://", USE_HTTPS),
    ("https:///a.jpg", USE_HTTPS),
    ("https://user:pw@cdn.example.com/a.jpg", USE_HTTPS),
    ("https://cdn.example.com\\@evil.example/a.jpg", USE_HTTPS),
    ("https://cdn.example.com/a b.jpg", USE_HTTPS),
    ("https://cdn.example.com/a\n.jpg", USE_HTTPS),
    ("https://cdn.example.com:notaport/a.jpg", USE_HTTPS),
    ("https://" + "a" * 2040, None),
    ("https://" + "a" * 2041, "https URLs are limited to 2048 characters"),
    ("file:covers/a.png", None),
    ("file:a.jpeg", None),
    ("file:covers/A.PNG", None),
    ("file:my photos/a b.webp", None),
    ("FILE:a.png", None),
    ("file:a.svg", IMAGE_EXT),
    ("file:a", IMAGE_EXT),
    ("file:.png", IMAGE_EXT),
    ("file:a.mp3", IMAGE_EXT),
    ("file:../x.png", FILE_REL),
    ("file:covers/../x.png", FILE_REL),
    ("file:/etc/x.png", FILE_REL),
    ("file://x.png", FILE_REL),
    ("file:.state/x.png", FILE_REL),
    ("file:.STATE/sessions/x.png", FILE_REL),
    ("file:.tmp/x.png", FILE_REL),
    ("file:./x.png", FILE_REL),
    ("file:./.state/x.png", FILE_REL),
    ("file:a//b.png", FILE_REL),
    ("file:dir/", FILE_REL),
    ("file:a\\b.png", FILE_REL),
    ("file:a\x01.png", FILE_REL),
    ("file:a\x00.png", "src contains a character that cannot be stored (a NUL or a lone surrogate)"),
    ("file:", FILE_REL),
    ("file:" + "a/" * 253 + "xx.png", None),
    ("file:" + "a/" * 253 + "xxx.png", FILE_REL),
    ("data:image/png;base64,iVBORw0KGgo=", None),
    ("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=", None),
    ("data:image/png;base64," + "A" * (65536 - len("data:image/png;base64,")), None),
    ("data:image/png;base64," + "A" * (65537 - len("data:image/png;base64,")), "data: sources are limited to 65536 characters"),
    ("data:text/html;base64,AAAA", "data: sources must start with data:image/<type>;base64,"),
    ("data:image/png,AAAA", "data: sources must start with data:image/<type>;base64,"),
    ("data:audio/mpeg;base64,AAAA", "data: sources must start with data:image/<type>;base64,"),
])
def test_image_sources(src, expected):
    assert _check("Image", "src", src) == expected


@pytest.mark.parametrize("src, expected", [
    ("https://cdn.example.com/audio/a.mp3", None),
    ("http://cdn.example.com/audio/a.mp3", USE_HTTPS),
    ("file:audio/rainy-day.flac", None),
    ("file:a.weba", None),
    ("file:a.png", AUDIO_EXT),
    ("file:../a.mp3", FILE_REL),
    ("file:.state/a.mp3", FILE_REL),
    ("data:audio/mpeg;base64,AAAA", None),
    ("data:image/png;base64,AAAA", "data: sources must start with data:audio/<type>;base64,"),
    ("data:audio/mpeg;base64," + "A" * 70000, "data: sources are limited to 65536 characters"),
])
def test_audio_sources(src, expected):
    assert _check("Audio", "src", src) == expected


def test_a_source_must_be_text():
    assert _check("Image", "src", 5) == "src must be text"
    assert _check("Audio", "src", None) == "src must be text"


@pytest.mark.parametrize("href, ok", [
    ("https://example.com/x", True), ("http://example.com/x?y=1#z", True), ("HTTP://Example.com", True),
    ("ftp://example.com", False), ("javascript:alert(1)", False), ("mailto:a@b.c", False), ("//example.com", False),
    ("example.com", False), ("https://", False), ("https://ex ample.com", False), ("https://u@example.com", False),
    ("https://example.com\\@evil.example", False), ("data:text/html,x", False), ("https://" + "a" * 2043, False), (5, False),
])
def test_link_hrefs(href, ok):
    assert (_check("Link", "href", href) is None) is ok
    if not ok:
        assert _check("Link", "href", href) == HREF


def test_html_size_is_counted_in_utf8_bytes():
    assert _check("Html", "html", "<b>hi</b>", max_html_bytes=9) is None
    assert _check("Html", "html", "<b>hi</b>", max_html_bytes=8) == "Html is 9 bytes; the limit is 8"
    assert _check("Html", "html", "é" * 10, max_html_bytes=19) == "Html is 20 bytes; the limit is 19"
    assert _check("Html", "html", 5) == "html must be text"
    assert _check("Html", "html", "x" * 65537) == "Html is 65537 bytes; the limit is 65536"


def test_a_nul_or_a_lone_surrogate_cannot_be_stored_anywhere():
    cannot = "contains a character that cannot be stored (a NUL or a lone surrogate)"
    assert _check("Text", "text", "a\x00b") == f"text {cannot}"
    assert _check("Button", "label", "\ud800") == f"label {cannot}"
    assert _check("Select", "options", ["ok", "bad\x00"]) == f"options {cannot}"
    assert _check("Select", "value", ["ok", "bad\udfff"]) == f"value {cannot}"
    assert _check("Html", "html", "<b>\x00</b>") == f"html {cannot}"
    assert _check("Text", "text", "caf\u00e9 \U0001F600") is None
