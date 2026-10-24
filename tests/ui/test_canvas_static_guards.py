"""Static guards on the canvas frame's code (pivot P5, Task 16; the plan's Global Constraints; the lead's ruling 6 on S7).

* The renderer never parses an agent's (or the console's) string as HTML in the outer frame, and runs no string as code.
* No file under ``ui/`` gives a frame ``allow-same-origin`` (one pre-existing docs test page, which frames the console's own bundle and no
  agent content, is named below with its reason).
* Every attribute the renderer writes is a literal name from a short list (never an ``on*`` handler, ``href``, ``src``, ``srcdoc`` or
  ``style``); a media ``src`` is only ever a URL ``CV.mediaDecision`` returned; no ``<style>``, ``<script>`` or ``<link>`` is created and
  no CSS text is written in the outer frame (the theme goes through ``CV.themeCss``'s rules, one ``setProperty`` per variable).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"
RENDERER = UI / "canvas" / "renderer.js"

#: A page that is not the canvas: the docs embed directive's test harness frames the console's own bundle (same-origin console code
#: and fixtures, never agent content) and needs ``allow-same-origin`` for it. Named so the exemption is seen, and checked below so it
#: cannot outlive the line it excuses.
SAME_ORIGIN_EXEMPT = {"docs-embed-directive-test.html": 'sandbox="allow-scripts allow-same-origin"'}

#: The attributes the renderer may write.
ALLOWED_ATTRIBUTES = {"type", "data-cv-id", "data-cv", "sandbox", "title", "aria-busy", "aria-label", "inert"}


def _code(path: Path) -> str:
    """The file without its comments (so a sentence about a sink is not a sink)."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(^|\s)/\*.*?\*/", r"\1", text, flags=re.DOTALL)   # a block comment starts a line or follows a space
    return re.sub(r"(?m)^\s*//.*$", "", text)


def test_the_renderer_never_parses_agent_strings_as_html():
    src = RENDERER.read_text(encoding="utf-8")
    assert not any(t in src for t in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"))


@pytest.mark.parametrize("path", [RENDERER], ids=["renderer"])
def test_no_canvas_script_parses_html_or_runs_a_string(path):
    code = _code(path)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "parseFromString",
                 "createContextualFragment", "eval(", "new Function", "Function(", "importScripts"):
        assert sink not in code, f"{path.name} uses {sink}"
    assert not re.search(r"set(?:Timeout|Interval)\(\s*[\"'`]", code), f"{path.name} passes a string to a timer"


def test_no_ui_file_grants_allow_same_origin():
    found = {p.relative_to(UI).as_posix() for p in UI.rglob("*")
             if p.suffix in (".js", ".jsx", ".html") and "allow-same-origin" in p.read_text(errors="ignore")}
    assert found <= set(SAME_ORIGIN_EXEMPT), sorted(found - set(SAME_ORIGIN_EXEMPT))
    for name, line in SAME_ORIGIN_EXEMPT.items():
        assert line in (UI / name).read_text(encoding="utf-8"), f"{name} no longer needs its exemption: remove it"
    assert not any("canvas" in name for name in SAME_ORIGIN_EXEMPT)


def test_every_attribute_the_renderer_writes_is_a_literal_from_the_list():
    code = _code(RENDERER)
    calls = re.findall(r"\.setAttribute\(\s*([^,]+?)\s*,", code)
    assert calls, "the scan found the renderer's attribute writes"
    names = set()
    for arg in calls:
        literal = re.fullmatch(r"\"([a-z-]+)\"", arg)
        assert literal, f"setAttribute with a computed name: {arg}"
        names.add(literal.group(1))
    assert names <= ALLOWED_ATTRIBUTES, sorted(names - ALLOWED_ATTRIBUTES)
    assert "createElement(\"style\")" not in code and "createElement(\"script\")" not in code and "createElement(\"link\")" not in code


def test_every_url_handler_and_srcdoc_the_renderer_writes_is_one_of_these():
    """Every write of a URL, a handler or CSS text in the renderer, listed. A media ``src`` is only a URL ``CV.mediaDecision``
    returned (``rec.url`` is one too, or the next minted URL a later decision returned); ``props.*`` is the renderer's own copy of an
    element's props, not the DOM."""
    code = _code(RENDERER)
    writes = set(re.findall(r"(\w+)\.(href|src|srcdoc|outerText|cssText|on\w+)\s*=(?!=)\s*([^;]+);", code))
    assert writes == {
        ("props", "src", 'str(p.src, MAX_SRC, "")'),
        ("props", "href", 'str(p.href, MAX_URL_CHARS, "")'),
        ("img", "src", "d.decision.url"),
        ("audio", "src", "d.decision.url"),
        ("media", "src", "rec.url"),
        ("host", "onmessage", "function (portEvent) { onHostMessage(host, portEvent)"),
    }, sorted(writes)
    assert set(re.findall(r"rec\.url\s*=(?!=)\s*([^;]+);", code)) <= {"d.decision.url", "rec.nextUrl", "null"}
    assert set(re.findall(r"rec\.nextUrl\s*=(?!=)\s*([^;]+);", code)) <= {"url", "null"}
    assert "keepOrSwap(rec, d.decision.url)" in code

