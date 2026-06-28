"""Server-side JSX bundler.

At app startup we transpile every ``<script type="text/babel">`` listed
in ``ui/index.html`` via Babel running inside an embedded V8 isolate
(``py-mini-racer``), concatenate the output, and stash it on
``app.state.jsx_bundle`` as a precomputed ``(etag, body)`` pair.

A bare ``GET /console/_app.js`` then returns that bundle directly —
**Babel never ships to the browser** and there is exactly one script
request instead of the previous 30+. Cold-load wall time falls from
"browser-side parse + transpile of ~700 KB of JSX" to "fetch one
gzipped bundle and execute it."

The concatenation preserves the script-tag order from ``index.html``;
since the existing UI relies on top-level ``const``/``function``
bindings being visible across files (Realm-shared script lexical
scope), we deliberately *do not* wrap each file in an IIFE — the
bundle behaves as if all scripts had been emitted as one long
``<script>`` tag.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)


# Match <script type="text/babel" src="…"></script> across line breaks
# and attribute reorderings. We intentionally only pick up the babel-
# typed entries; native <script src="vendor/react.min.js"> tags stay
# in index.html as their own requests.
_SCRIPT_RE = re.compile(
    r'<script[^>]*?\btype="text/babel"[^>]*?\bsrc="([^"]+)"[^>]*?>\s*</script>',
    re.IGNORECASE,
)


class JSXBundler:
    """Holds the V8 isolate + Babel; one instance per app."""

    def __init__(self, *, ui_dir: Path, babel_source: str) -> None:
        # Importing py_mini_racer is cheap; constructing MiniRacer
        # spins up V8 (~50 ms). Loading Babel into it is the dominant
        # one-time cost (~150 ms).
        from py_mini_racer import MiniRacer

        self._ui_dir = ui_dir
        self._ctx = MiniRacer()
        self._ctx.eval(babel_source)
        # Babel plugin: rewrite top-level `const`/`let` to `var`.
        #
        # Why: the legacy multi-script layout relied on each
        # ``<script type="text/babel">`` getting its own Script
        # Record (Babel Standalone uses indirect eval), so different
        # files could each declare ``const apiFetch = …`` at top
        # level without colliding. Concatenating them into one
        # script collapses those records and the second declaration
        # throws ``SyntaxError: Identifier 'apiFetch' has already
        # been declared``.
        #
        # Turning top-level ``const``/``let`` into ``var`` puts the
        # binding on the global object instead of the script's
        # DeclarativeRecord: redeclarations silently overwrite, and
        # bare-name JSX refs like ``<Icon />`` (which would be
        # ``React.createElement(Icon, …)``) still resolve via the
        # global object across files. Block-scoped ``const``/``let``
        # inside functions / loops / blocks are left untouched, so
        # the visitor only touches Program.body entries.
        self._ctx.eval(
            "Babel.registerPlugin('primer-flatten-toplevel-bindings',"
            " function() {"
            "  return {"
            "   visitor: {"
            "    Program: function(path) {"
            "     for (var i = 0; i < path.node.body.length; i++) {"
            "      var s = path.node.body[i];"
            "      if (s.type === 'VariableDeclaration'"
            "          && (s.kind === 'const' || s.kind === 'let')) {"
            "       s.kind = 'var';"
            "      }"
            "     }"
            "    }"
            "   }"
            "  };"
            " });"
        )

    def _transform(self, source: str, filename: str) -> str:
        # Babel.transform returns {code, map, ast}. We only want code.
        # 'react' preset handles JSX; the registered plugin flattens
        # top-level const/let bindings (see __init__). Modern browsers
        # handle the rest of the syntax natively, so preset-env stays
        # off — keeps the boot path cheap.
        expr = (
            "Babel.transform("
            + json.dumps(source)
            + ", {presets: ['react'],"
            "    plugins: ['primer-flatten-toplevel-bindings'],"
            "    filename: " + json.dumps(filename) + ","
            "    sourceMaps: false, compact: false}).code"
        )
        return self._ctx.eval(expr) or ""

    def parse_script_order(self, index_html: str) -> list[str]:
        return _SCRIPT_RE.findall(index_html)

    def build(self) -> tuple[str, bytes]:
        """Read ui/index.html, transpile + concat. Returns (etag, body)."""
        index_html = (self._ui_dir / "index.html").read_text()
        order = self.parse_script_order(index_html)
        parts: list[str] = []
        skipped = 0
        for rel in order:
            path = (self._ui_dir / rel).resolve()
            if not path.is_file():
                logger.warning("jsx_bundle: missing %s — skipping", rel)
                skipped += 1
                continue
            src = path.read_text()
            try:
                transpiled = self._transform(src, rel)
            except Exception as exc:
                logger.error("jsx_bundle: babel failed on %s: %s", rel, exc)
                raise
            parts.append(f"\n/* === {rel} === */\n")
            parts.append(transpiled)
        bundle_str = "\n".join(parts)
        body = bundle_str.encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        logger.info(
            "jsx_bundle: built %d files into %d bytes (etag=%s, skipped=%d)",
            len(order) - skipped, len(body), etag, skipped,
        )
        return etag, body


@cache
def build_jsx_bundle(ui_dir: Path) -> tuple[str, bytes]:
    """One-shot helper used at app startup.

    Returns ``("", b"")`` when the UI dir is missing or Babel can't be
    found — the route handler treats an empty body as "no bundle
    available" and returns 404 instead of crashing.

    Memoized on ``ui_dir``: the bundle is a pure function of the UI directory
    contents, and the ~700 KB Babel/V8 transpile is otherwise recomputed from
    scratch on every ``create_app()`` and every ``tests/ui`` transpile check
    (dozens of identical builds per process). The per-``docs_url`` preamble +
    ETag are applied by the caller after this returns, so caching the raw
    bundle does not affect docs-url behaviour. Production builds the bundle
    once at startup, so the cache is invisible there; a process that needs a
    fresh build (e.g. after editing UI files) can call
    ``build_jsx_bundle.cache_clear()``.
    """
    if not ui_dir.is_dir():
        return "", b""
    babel_path = ui_dir / "vendor" / "babel.min.js"
    if not babel_path.is_file():
        logger.warning(
            "jsx_bundle: %s missing — cannot precompile JSX, "
            "browser would need Babel Standalone",
            babel_path,
        )
        return "", b""
    bundler = JSXBundler(ui_dir=ui_dir, babel_source=babel_path.read_text())
    return bundler.build()
