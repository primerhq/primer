"""Syntax-check the scripts inside an Html element's body (Pivot P5, Task 4; design 8.6).

An agent's Html body may carry scripts. At ``canvas_apply`` time they are checked
so a typo comes back as a precise error instead of a blank frame:

* a classic inline script is compiled by V8 and never run: the source reaches the
  engine as a string argument to ``new Function(s)``, which parses the body and
  does not call it, and V8 itself refuses a body that tries to close the function
  early. The source is never concatenated into code, so it cannot escape the
  probe. V8 reports no position inside the script, so a problem names the script
  by its number in the document.
* an external script must be https and come from one of four CDNs. An HTML script
  names its URL in ``src`` and an SVG one in ``href`` or ``xlink:href``; the check
  does not know which kind a script is, so it judges each of the three that is
  present. The frame's CSP is what enforces that in the browser; this gives the
  agent the error early.

Known limit: ``new Function`` accepts a top-level ``return`` that a classic script
rejects. Module scripts and data blocks (``type=module``, ``application/json``,
``importmap`` ...) are not compiled. Pure: no I/O besides the throwaway engine.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from urllib.parse import urlsplit

#: Where an Html element's external scripts may come from. The frame CSP (Task 13) reads this.
SCRIPT_HOSTS: tuple[str, ...] = ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com", "esm.sh")

_ALLOWED_TEXT = f"{', '.join(SCRIPT_HOSTS[:-1])} and {SCRIPT_HOSTS[-1]}"
_MAX_MESSAGE = 200
_ENGINE_TIMEOUT_MS = 1000
_ENGINE_MEMORY_BYTES = 128 * 1024 * 1024
_PROBE = "(function (s) { try { new Function(s); return null; } catch (e) { return String(e.message); } })("

#: The MIME types a browser runs as a classic script (HTML Standard, "JavaScript MIME type").
_CLASSIC_TYPES = frozenset({
    "application/ecmascript",
    "application/javascript",
    "application/x-ecmascript",
    "application/x-javascript",
    "text/ecmascript",
    "text/javascript",
    "text/javascript1.0",
    "text/javascript1.1",
    "text/javascript1.2",
    "text/javascript1.3",
    "text/javascript1.4",
    "text/javascript1.5",
    "text/jscript",
    "text/livescript",
    "text/x-ecmascript",
    "text/x-javascript",
})
_URL_STRIP = "".join(chr(c) for c in range(0x21))
#: Where a script names the URL it loads: ``src`` in HTML, ``href`` or ``xlink:href`` in SVG.
_URL_ATTRIBUTES = ("src", "href", "xlink:href")


class _Scripts(HTMLParser):
    """Collects every ``<script>``: its attributes (the first of a repeated one wins) and its text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self._attrs: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        self._finish()
        first: dict[str, str | None] = {}
        for name, value in attrs:
            first.setdefault(name, value)
        self._attrs = first

    def handle_data(self, data: str) -> None:
        if self._attrs is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._finish()

    def close(self) -> None:
        super().close()
        self._finish()  # a script that is never closed runs to the end of the document

    def _finish(self) -> None:
        if self._attrs is not None:
            self.scripts.append((self._attrs, "".join(self._text)))
            self._attrs = None
            self._text = []


def _is_classic(type_attr: str | None) -> bool:
    essence = (type_attr or "").split(";", 1)[0].strip().lower()
    return essence == "" or essence in _CLASSIC_TYPES


def _check_url(number: int, attribute: str, value: str | None) -> list[str]:
    try:
        parts = urlsplit((value or "").strip(_URL_STRIP))
    except ValueError:
        return [f"script {number} {attribute} must be https"]
    if parts.scheme.lower() != "https" or not parts.netloc:
        return [f"script {number} {attribute} must be https"]
    # Judge the authority as written: a backslash or userinfo that one parser reads
    # as a path or a login is a different host to another, so only a plain host passes.
    netloc = parts.netloc.lower()
    host = netloc[:-4] if netloc.endswith(":443") else netloc
    if host not in SCRIPT_HOSTS:
        return [f"script {number} loads from {netloc}; only {_ALLOWED_TEXT} are allowed"]
    return []


class _Compiler:
    """One throwaway V8 context that only ever parses."""

    def __init__(self) -> None:
        from py_mini_racer import JSEvalException, MiniRacer

        self.failure = JSEvalException
        self._ctx = MiniRacer()
        self._ctx.set_hard_memory_limit(_ENGINE_MEMORY_BYTES)

    def problem(self, source: str) -> str | None:
        message = self._ctx.eval(_PROBE + json.dumps(source) + ")", timeout=_ENGINE_TIMEOUT_MS)
        if message is None:
            return None
        message = str(message)
        return message if len(message) <= _MAX_MESSAGE else message[: _MAX_MESSAGE - 3] + "..."

    def close(self) -> None:
        self._ctx.close()


def check_html_scripts(html: str) -> list[str]:
    """Problems with the scripts in ``html``, numbered from 1 in document order.

    An empty list means every external script is allowed and every classic inline
    script parses.
    """
    parser = _Scripts()
    parser.feed(html)
    parser.close()
    problems: list[str] = []
    compiler: _Compiler | None = None
    try:
        for number, (attrs, text) in enumerate(parser.scripts, start=1):
            for attribute in _URL_ATTRIBUTES:
                if attribute in attrs:
                    problems += _check_url(number, attribute, attrs[attribute])
            if "src" not in attrs and _is_classic(attrs.get("type")):
                if compiler is None:
                    compiler = _Compiler()
                try:
                    message = compiler.problem(text)
                except compiler.failure:
                    problems.append(f"script {number} could not be checked: the syntax check failed")
                else:
                    if message is not None:
                        problems.append(f"script {number} does not parse: {message}")
    finally:
        if compiler is not None:
            compiler.close()
    return problems


__all__ = ["SCRIPT_HOSTS", "check_html_scripts"]
