"""Static guards on the canvas frame's code (pivot P5, Tasks 16 and 17; the plan's Global Constraints; the lead's rulings 2 and 6 on S7).

* The renderer never parses an agent's (or the console's) string as HTML in the outer frame, and runs no string as code: agent HTML exists
  only as an Html element's ``srcdoc``, which is written once, from ``CV.htmlSrcdoc``.
* No file under ``ui/`` gives a frame ``allow-same-origin`` (one pre-existing docs test page, which frames the console's own bundle and no
  agent content, is named below with its reason).
* Every attribute the renderer writes is a literal name from a short list (never an ``on*`` handler, ``href``, ``src``, ``srcdoc`` or
  ``style``); a media ``src`` is only ever a URL ``CV.mediaDecision`` returned; no ``<style>``, ``<script>`` or ``<link>`` is created and
  no CSS text is written in the outer frame (the theme goes through ``CV.themeCss``'s rules, one ``setProperty`` per variable).
* Ruling 2 (audit G8-06): every Html frame inherits the header's policy, and the one ``<meta>`` policy ``CV.htmlSrcdoc`` adds only
  narrows it: it names only ``img-src``, ``media-src`` and ``font-src``, and every source it lists is one the header's same directive
  already allows. The browser pin (a script from a host that is not allowed raises a CSP violation inside an Html frame) is Task 26's
  journey (lead-scheduled).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from primer.canvas.frame import frame_csp

UI = Path(__file__).resolve().parents[2] / "ui"
RENDERER = UI / "canvas" / "renderer.js"
BOOTSTRAP = UI / "canvas" / "html-bootstrap.js"

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


@pytest.mark.parametrize("path", [RENDERER, BOOTSTRAP], ids=["renderer", "bootstrap"])
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
    """Every write of a URL, a handler, a srcdoc or CSS text in the renderer, listed. A media ``src`` is only a URL ``CV.mediaDecision``
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
        ("frame", "srcdoc", "doc0"),
        ("host", "onmessage", "function (portEvent) { onHostMessage(host, portEvent)"),
        ("port", "onmessage", "function (ev) { onHtmlMessage(rec, port, ev)"),
    }, sorted(writes)
    assert set(re.findall(r"rec\.url\s*=(?!=)\s*([^;]+);", code)) <= {"d.decision.url", "rec.nextUrl", "null"}
    assert set(re.findall(r"rec\.nextUrl\s*=(?!=)\s*([^;]+);", code)) <= {"url", "null"}
    assert "keepOrSwap(rec, d.decision.url)" in code


def test_the_srcdoc_is_written_once_from_htmlSrcdoc():
    code = _code(RENDERER)
    assert re.findall(r"\.srcdoc\s*=\s*([^;]+);", code) == ["doc0"]
    assert re.search(r"var doc0 = htmlSrcdoc\(", code)
    assert "setAttribute(\"srcdoc\"" not in code and "setAttribute(\"src\"" not in code
    assert code.count('frame.setAttribute("sandbox", "allow-scripts allow-forms");') == 1


def test_the_renderer_writes_one_policy_and_it_is_the_narrowing_one():
    """Ruling 2 on S7: the only ``http-equiv`` in the frame's code is the Html frame's narrowing media policy, in ``CV.htmlSrcdoc``."""
    code = _code(RENDERER)
    assert code.count("http-equiv") == 1
    assert '<meta http-equiv="Content-Security-Policy" content="\' + csp + \'">\'' in code
    assert "http-equiv" not in _code(BOOTSTRAP)


def _directives(policy: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            assert words[0] not in out, f"{words[0]} twice"
            out[words[0]] = words[1:]
    return out


def _allowed_by(source: str, header_sources: list[str]) -> bool:
    if source in header_sources:
        return True
    return source.startswith("https://") and "https:" in header_sources


@pytest.fixture(scope="module")
def renderer():
    py_mini_racer = pytest.importorskip("py_mini_racer")
    context = py_mini_racer.MiniRacer()
    context.eval("var window = globalThis;")
    context.eval(RENDERER.read_text(encoding="utf-8"))
    yield context
    context.close()


@pytest.mark.parametrize("settings, own", [
    ({"media_policy": "none", "media_allowlist": []}, "http://h:9000"),
    ({"media_policy": "none", "media_allowlist": []}, None),
    ({"media_policy": "allowlist", "media_allowlist": ["a.example.com", "*.cdn.example"]}, "http://primer.local:9000"),
    ({"media_policy": "allowlist", "media_allowlist": ["a.example.com"]}, None),
    ({"media_policy": "allowlist", "media_allowlist": []}, None),
    ({"media_policy": "any-https", "media_allowlist": []}, "http://h:9000"),
    ({"media_policy": "unknown", "media_allowlist": ["a.example.com"]}, None),
])
def test_the_html_frames_policy_only_narrows_the_headers(renderer, settings, own):
    """Ruling 2 on S7 (audit G8-06): CV.htmlSrcdoc never sets a weaker policy than the shell's."""
    meta = json.loads(renderer.eval(f"JSON.stringify(CV.metaCsp({json.dumps(settings)}, {json.dumps(own or '')}))"))
    header = _directives(frame_csp(own_origin=own))
    narrowing = _directives(meta)
    assert narrowing.pop("webrtc") == ["'block'"], "every Html frame refuses WebRTC (the lead's ruling on #925); the header allows it"
    if settings["media_policy"] == "any-https":
        assert narrowing == {}, "under any-https the header's media rule holds"
    else:
        assert set(narrowing) == {"img-src", "media-src", "font-src"}, "only the media directives, never script-src, default-src or sandbox"
    assert "webrtc" not in header
    for name, sources in narrowing.items():
        assert sources, f"{name} is empty"
        for source in sources:
            assert not source.startswith("'"), f"{name} {source}: a keyword source"
            assert _allowed_by(source, header[name]), f"{name} {source} is not allowed by the header's {name} {header[name]}"
    doc = json.loads(renderer.eval(
        f"JSON.stringify(CV.htmlSrcdoc('<p>agent</p>', {{settings: {json.dumps(settings)}, ownOrigin: {json.dumps(own or '')}, theme: {{}}, "
        "bootstrap: 'B'}))"
    ))
    assert doc.count("http-equiv") == 1 and f'content="{meta}"' in doc
    assert doc.index("http-equiv") < doc.index("<p>agent</p>")
