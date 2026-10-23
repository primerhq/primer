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

Call it off the event loop (``await asyncio.to_thread(check_html_scripts, html)``): it
blocks until V8 has answered, milliseconds as a rule and a second at the very most for
each script. It is safe wherever it is called all the same. mini-racer adopts the running
loop of the thread that makes an engine and then refuses a timeout, so on a thread that
has a running loop the engine is made and used on a worker thread instead. Whatever goes
wrong with the engine, that or anything else, is a problem in the result ("could not be
checked"), never an exception: the check fails closed.

This is an EARLY WARNING for the agent, not a gate (lead ruling on audit G8-06). The frame's CSP (Task 13), inherited
by every Html ``srcdoc`` frame, is the only control in the browser. That CSP allows ``'unsafe-inline'``, so anything
this check does not see and that is inline (an event handler, a ``javascript:`` URL, a script a browser parses where
this check sees none) RUNS; only external loads are held to the CSP's hosts.

How the scripts are found. ``html.parser`` reads the body with scripting ON, as the frame does (a ``<noscript>`` is
raw text, so what follows its end tag is markup). It still has no foreign-content rules (inside ``<svg>``, a
``<style>``, ``<iframe>``, ``<xmp>``, ``<textarea>`` or ``<title>`` holds markup in a browser and text here), and it
ends a script at its first ``</script>`` where a browser in the double-escaped state does not, and it reads
``<script/>`` as an empty script where a browser ignores the slash and runs what follows. So such a tag is not
counted as a script it read, and the check fails closed: when the body holds more ``<script`` (in any case) than the scripts the parser found, it is refused, because
a browser may run what the check cannot see. That also refuses the word in text, a comment, an attribute or a JS
string; the agent splits it (``'<scr' + 'ipt'``).

Known limits. ``new Function`` accepts a top-level ``return`` that a classic script rejects. Module scripts and data
blocks (``type=module``, ``application/json``, ``importmap`` ...) are not compiled. The check looks at nothing that is
not a ``<script>``, so it cannot see:

* event-handler attributes (``onerror="..."``);
* ``javascript:`` URLs;
* ``import`` and ``import()`` in an inline module (or, for ``import()``, a classic script);
* a script inside a nested ``<iframe srcdoc>`` whose markup is written with character references (``&lt;script``);
* a whole document encoded in a URL: an ``<iframe>``, ``<object>`` or ``<embed>`` whose ``src`` or ``data`` is a
  ``data:`` URL, or ``<meta http-equiv="refresh">`` to a ``data:`` URL.

Pure: no I/O besides the throwaway engine.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urlsplit

#: Where an Html element's external scripts may come from. The frame CSP (Task 13) reads this.
SCRIPT_HOSTS: tuple[str, ...] = ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com", "esm.sh")

_ALLOWED_TEXT = f"{', '.join(SCRIPT_HOSTS[:-1])} and {SCRIPT_HOSTS[-1]}"
_MAX_MESSAGE = 200
_ENGINE_TIMEOUT_SEC = 1.0
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
    """Collects every ``<script>``: its attributes (the first of a repeated one wins), its text, and whether it counts as a script
    the check could read (a self-closed ``<script/>`` does not; see :meth:`handle_startendtag`)."""

    def __init__(self) -> None:
        try:
            # Scripting ON, as in the frame: <noscript> is raw text (audit G8-06). A Python without the flag still fails closed on the count.
            super().__init__(convert_charrefs=True, scripting=True)
        except TypeError:  # pragma: no cover - CPython before the flag
            super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[dict[str, str | None], str, bool]] = []
        self._attrs: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        self._finish()
        self._attrs = _first_of_each(attrs)

    def handle_data(self, data: str) -> None:
        if self._attrs is not None:
            self._text.append(data)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # ``<script/>`` is NOT an empty script to a browser: in HTML the slash is ignored and what follows is the script's text,
        # which html.parser reads as markup. Its URLs are still judged, but it is not counted as a script the check read, so the
        # '<script' count is one higher than the scripts found and the body fails closed (review N1 on #856). Any other
        # self-closed tag is no script.
        if tag != "script":
            return
        self._finish()
        self.scripts.append((_first_of_each(attrs), "", False))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._finish()

    def close(self) -> None:
        super().close()
        # A script that is never closed is still checked. A browser does not run it, so this is only stricter.
        self._finish()

    def _finish(self) -> None:
        if self._attrs is not None:
            self.scripts.append((self._attrs, "".join(self._text), True))
            self._attrs = None
            self._text = []


def _first_of_each(attrs: list[tuple[str, str | None]]) -> dict[str, str | None]:
    first: dict[str, str | None] = {}
    for name, value in attrs:
        first.setdefault(name, value)
    return first


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
        from py_mini_racer import MiniRacer

        self._ctx = MiniRacer()
        try:
            self._ctx.set_hard_memory_limit(_ENGINE_MEMORY_BYTES)
        except Exception:
            self._ctx.close()
            raise

    def problem(self, source: str) -> str | None:
        message = self._ctx.eval(_PROBE + json.dumps(source) + ")", timeout_sec=_ENGINE_TIMEOUT_SEC)
        if message is None:
            return None
        message = str(message)
        return message if len(message) <= _MAX_MESSAGE else message[: _MAX_MESSAGE - 3] + "..."

    def close(self) -> None:
        self._ctx.close()


def _unchecked(number: int) -> str:
    return f"script {number} could not be checked: the syntax check failed"


def _hidden(raw: int, found: int) -> str | None:
    """The problem for a body with ``raw`` ``<script`` in it of which the parser read only ``found`` as scripts, else ``None``."""
    hidden = raw - found
    if hidden <= 0:
        return None
    verb = "is" if hidden == 1 else "are"
    return (
        f"{hidden} of the {raw} '<script' in the html {verb} not a script the check can read; write each script as a plain <script> "
        "element and split '<script' in any text or string ('<scr' + 'ipt')"
    )


def _compile_scripts(scripts: list[tuple[int, int, str]]) -> dict[int, str]:
    """The problem with each inline script that has one, by task index (with ``number`` as its script number in its document).

    One engine serves every script. Whatever goes wrong with it (it cannot start, it times
    out, it runs out of memory, it raises something nobody expected) is "could not be
    checked" for the scripts it did not answer for: the check fails closed. Never raises.
    """
    try:
        compiler = _Compiler()
    except Exception:
        return {idx: _unchecked(number) for idx, number, _ in scripts}
    problems: dict[int, str] = {}
    try:
        for idx, number, source in scripts:
            try:
                message = compiler.problem(source)
            except Exception:
                problems[idx] = _unchecked(number)
            else:
                if message is not None:
                    problems[idx] = f"script {number} does not parse: {message}"
    finally:
        try:
            compiler.close()
        except Exception:  # the answers are already in; a failed close cannot change them
            pass
    return problems


def _compile(scripts: list[tuple[int, int, str]]) -> dict[int, str]:
    """:func:`_compile_scripts`, on a worker thread when this thread has a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _compile_scripts(scripts)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="canvas-script-check") as pool:
            return pool.submit(_compile_scripts, scripts).result()
    except Exception:
        return {idx: _unchecked(number) for idx, number, _ in scripts}


def check_html_scripts_many(bodies: Sequence[str]) -> dict[str, list[str]]:
    """Problems with the scripts in each distinct body of ``bodies``, using at most ONE V8 engine for the whole batch."""
    unique = list(dict.fromkeys(bodies))
    found_by_body: dict[str, dict[int, list[str]]] = {}
    hidden_by_body: dict[str, str] = {}
    inline_tasks: list[tuple[int, int, str, str]] = []
    for html in unique:
        parser = _Scripts()
        parser.feed(html)
        parser.close()
        found: dict[int, list[str]] = {}
        for number, (attrs, text, read) in enumerate(parser.scripts, start=1):
            for attribute in _URL_ATTRIBUTES:
                if attribute in attrs:
                    found.setdefault(number, []).extend(_check_url(number, attribute, attrs[attribute]))
            if read and "src" not in attrs and _is_classic(attrs.get("type")):
                inline_tasks.append((len(inline_tasks), number, text, html))
        found_by_body[html] = found
        hidden = _hidden(html.lower().count("<script"), sum(1 for _attrs, _text, read in parser.scripts if read))
        if hidden is not None:
            hidden_by_body[html] = hidden
    if inline_tasks:
        compiled = _compile([(idx, number, text) for idx, number, text, _ in inline_tasks])
        for idx, problem in compiled.items():
            _, number, _, html = inline_tasks[idx]
            found_by_body[html].setdefault(number, []).append(problem)
    return {
        html: [problem for number in sorted(found) for problem in found[number]] + ([hidden_by_body[html]] if html in hidden_by_body else [])
        for html, found in found_by_body.items()
    }


def check_html_scripts(html: str) -> list[str]:
    """Problems with the scripts in ``html``, numbered from 1 in document order.

    An empty list means every external script is allowed and every classic inline
    script parses.
    """
    return check_html_scripts_many([html])[html]


check_html_scripts.many = check_html_scripts_many  # type: ignore[attr-defined]


__all__ = ["SCRIPT_HOSTS", "check_html_scripts", "check_html_scripts_many"]
