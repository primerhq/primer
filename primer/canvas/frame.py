"""The canvas frame shell and its headers (pivot P5, Task 13; design 8.6, plan Decisions 10 and 11).

The console never puts canvas content into its own DOM: it mounts ``<iframe src="/canvas/frame?v=<version>" sandbox="allow-scripts
allow-forms">``, a static page served outside ``/v1`` with its own policy. The page is ``ui/canvas/frame.html`` with the frame's CSS and
the trusted renderer inlined into it, and the Html bootstrap as a JavaScript string (``window.CV_BOOTSTRAP``, Task 17: the renderer
writes it into each Html element's srcdoc, it never runs in the shell), so the shell loads nothing from anywhere, and its version is the
first 16 hex characters of the page's sha256: a console that names the current version gets a page cached for a year, any other ``v``
gets one that is never cached.

**The CSP header is the canvas's only script control (audit G8-06; the lead's ruling 5 on S6).** The Html script check
(``primer.canvas.html_check``) is an early warning for the agent, never a gate. The header sandboxes the frame without its origin
(``sandbox allow-scripts allow-forms``: an opaque origin, no cookies, no storage of the console's), allows scripts only inline and from
the four CDNs of :data:`~primer.canvas.html_check.SCRIPT_HOSTS`, and connects nowhere. Every Html element is a ``srcdoc`` frame of this
page, and an ``about:srcdoc`` document inherits the policy container of the document that made it, so the header binds every Html frame
too. Each Html frame may only add its own, stricter media policy (Decision 10): the renderer's ``CV.metaCsp`` names only ``img-src``,
``media-src`` and ``font-src``, each a subset of the header's, and a second policy is enforced alongside the first, never instead of it
(``tests/ui/test_canvas_static_guards.py`` pins the subset). The shell itself carries no second policy, nonce or hash, and no file of the
frame makes a frame from a ``data:`` or ``blob:`` URL (``tests/canvas/test_frame.py`` scans every file of :data:`FRAME_ASSETS`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from primer.canvas.html_check import SCRIPT_HOSTS

#: Every file of the frame, under ``ui/canvas/``: the template and what it inlines. A file added there is added here, so the scan of
#: ``tests/canvas/test_frame.py`` reads it (``html-bootstrap.js`` is Task 17's).
FRAME_ASSETS: tuple[str, ...] = ("frame.html", "frame.css", "renderer.js", "html-bootstrap.js")

#: ``placeholder in frame.html -> the file inlined there``. A placeholder the template must carry.
_REQUIRED = (("/*__CANVAS_CSS__*/", "frame.css"), ("/*__CANVAS_RENDERER__*/", "renderer.js"))
#: ``placeholder -> the file inlined there as a JavaScript string`` (the statement ``window.CV_BOOTSTRAP = "...";``), never as code.
_AS_STRING = (("/*__CANVAS_HTML_BOOTSTRAP__*/", "html-bootstrap.js", "CV_BOOTSTRAP"),)

_SCRIPT_SOURCES = " ".join(f"https://{host}" for host in SCRIPT_HOSTS)

#: A Host header that names a host: a DNS name or an IPv4 address, or a bracketed IPv6 address, with an optional port. Anything else
#: (a space, a ``;``) would write into the policy, so it adds no origin.
_HOST_RE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])(?::[0-9]{1,5})?")

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_NONE = "no-store"


@dataclass(frozen=True)
class FrameShell:
    """The page the frame route serves: its bytes and its version (the first 16 hex characters of their sha256)."""

    body: bytes
    version: str


_cache: dict[Path, tuple[tuple[tuple[str, int, int], ...], FrameShell]] = {}


def _stamp(canvas_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """What the shell is built from, as (file, mtime, size): a changed file builds the shell again."""
    stamps = []
    for name in FRAME_ASSETS:
        path = canvas_dir / name
        if path.is_file():
            stat = path.stat()
            stamps.append((name, stat.st_mtime_ns, stat.st_size))
    return tuple(stamps)


def js_string(source: str) -> str:
    """``source`` as a JavaScript string literal that is safe inside a ``<script>`` element: JSON (every character outside ASCII escaped,
    U+2028 and U+2029 included) with each ``<`` written ``\\u003c``, so the literal can never close its element or open a comment."""
    return json.dumps(source, ensure_ascii=True).replace("<", "\\u003c")


def build_frame_shell(ui_dir: Path) -> FrameShell:
    """The frame shell of ``ui_dir`` (the console's ``ui/``), built once per set of files and cached per path.

    Raises ``ValueError`` when ``ui/canvas/frame.html`` lacks a placeholder it must carry, and ``OSError`` when a file is missing.
    """
    canvas_dir = Path(ui_dir) / "canvas"
    key = canvas_dir.resolve()
    stamp = _stamp(canvas_dir)
    cached = _cache.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    template = (canvas_dir / "frame.html").read_text(encoding="utf-8")
    inlined: dict[str, str] = {}
    for placeholder, name in _REQUIRED:
        if placeholder not in template:
            raise ValueError(f"ui/canvas/frame.html has no {placeholder} placeholder for {name}")
        inlined[placeholder] = (canvas_dir / name).read_text(encoding="utf-8")
    for placeholder, name, variable in _AS_STRING:
        if placeholder not in template:
            raise ValueError(f"ui/canvas/frame.html has no {placeholder} placeholder for {name}")
        inlined[placeholder] = f"window.{variable} = {js_string((canvas_dir / name).read_text(encoding='utf-8'))};"
    # One pass over the template only: what a file inlines is never read for another placeholder.
    page = re.sub("|".join(re.escape(p) for p in inlined), lambda m: inlined[m.group(0)], template)
    body = page.encode("utf-8")
    shell = FrameShell(body=body, version=hashlib.sha256(body).hexdigest()[:16])
    _cache[key] = (stamp, shell)
    return shell


def frame_url(ui_dir: Path) -> str:
    """The URL the console mounts the frame at: the shell's current version, so a deploy is never served from a stale cache."""
    return f"/canvas/frame?v={build_frame_shell(ui_dir).version}"


def frame_csp(*, own_origin: str | None) -> str:
    """The frame's policy, exactly the design's; ``own_origin`` (a plain-http request's own origin, Decision 11) joins ``img-src`` and
    ``media-src`` so a workspace file's capability URL loads on an http install."""
    extra = f" {own_origin}" if own_origin else ""
    return (
        "sandbox allow-scripts allow-forms; default-src 'none'; "
        f"script-src 'unsafe-inline' {_SCRIPT_SOURCES}; style-src 'unsafe-inline'; "
        f"img-src https: data: blob:{extra}; media-src https: data: blob:{extra}; font-src data: https:; connect-src 'none'; "
        "form-action 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'"
    )


def frame_headers(*, version_ok: bool, own_origin: str | None) -> dict[str, str]:
    """The frame response's headers: its policy, framed only by the console's origin, no referrer, no sniffing, and cached for a year
    only when the request named the current version."""
    return {
        "Content-Security-Policy": frame_csp(own_origin=own_origin),
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": CACHE_IMMUTABLE if version_ok else CACHE_NONE,
    }


def own_origin_of(scheme: str, host: str | None) -> str | None:
    """The request's own origin for :func:`frame_csp`: ``http://<host>`` on plain http when ``host`` is a host name, else ``None``."""
    if scheme != "http" or not host or _HOST_RE.fullmatch(host) is None:
        return None
    return f"http://{host}"


__all__ = [
    "CACHE_IMMUTABLE",
    "CACHE_NONE",
    "FRAME_ASSETS",
    "FrameShell",
    "build_frame_shell",
    "frame_csp",
    "frame_headers",
    "frame_url",
    "js_string",
    "own_origin_of",
]
