"""Structural-presence checks for the Studio integrated terminal (P7).

The terminal is a collapsible bottom panel spanning the Studio center
column (docs/superpowers/specs/2026-07-01-studio-design.md §4.5 + §6.5):
xterm.js + the fit addon (vendored UMD, like g6.min.js), one WebSocket per
terminal tab against the locked server contract
`WS /v1/workspaces/{wid}/terminal?cols=&rows=`.

These tests assert the vendor files are real + registered, the component
exists + is wired into the center column, the WS URL + binary/JSON frame
protocol are implemented as specified, the required data-testids are
present, and the bundle still transpiles. They do NOT render React or open
a browser (the ui/ suite is static-source + bundle-build only, matching
test_studio_shell.py / test_g6_is_gr_canvas.py) — except for one targeted
check that loads the vendored UMD bundles into the same V8 isolate the
server-side JSX bundler already depends on (py_mini_racer) to verify the
actual exported global shape, not just a minifier-fragile substring guess.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
VENDOR = UI / "vendor"
INDEX = UI / "index.html"
STYLES = UI / "styles.css"
MANIFEST = VENDOR / "MANIFEST.md"
STUDIO = UI / "components" / "studio.jsx"
TERMINAL = UI / "components" / "studio-terminal.jsx"

XTERM_JS = VENDOR / "xterm.min.js"
XTERM_CSS = VENDOR / "xterm.min.css"
XTERM_FIT = VENDOR / "xterm-addon-fit.min.js"


def _terminal_src() -> str:
    return TERMINAL.read_text(encoding="utf-8")


def _studio_src() -> str:
    return STUDIO.read_text(encoding="utf-8")


def _index_text() -> str:
    return INDEX.read_text(encoding="utf-8")


def _babel_order() -> list[str]:
    out: list[str] = []
    for line in _index_text().splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_xterm_vendor_files_exist_and_look_real() -> None:
    # Real assets, not HTML error pages or empty stubs.
    js = XTERM_JS.read_text(encoding="utf-8")
    css = XTERM_CSS.read_text(encoding="utf-8")
    fit = XTERM_FIT.read_text(encoding="utf-8")

    assert XTERM_JS.stat().st_size > 100_000, "xterm.min.js looks truncated"
    assert XTERM_FIT.stat().st_size > 500, "xterm-addon-fit.min.js looks truncated"
    assert XTERM_CSS.stat().st_size > 1_000, "xterm.min.css looks truncated"

    assert not js.lstrip().startswith("<"), "xterm.min.js looks like an HTML error page"
    assert not fit.lstrip().startswith("<"), "xterm-addon-fit.min.js looks like an HTML error page"
    assert not css.lstrip().startswith("<"), "xterm.min.css looks like an HTML error page"

    assert "Terminal" in js
    assert "FitAddon" in fit
    assert "xterm" in css.lower()


def test_xterm_umd_globals_match_component_usage() -> None:
    """Load the vendored UMD bundles into the same V8 isolate the server-side
    JSX bundler (primer.api._jsx_bundle) already embeds via py_mini_racer, as
    a plain <script> tag would (no CommonJS `exports`/`module`, no AMD
    `define`) — then assert the resolved global shape matches exactly what
    studio-terminal.jsx constructs.

    xterm.min.js's UMD wrapper spreads its module exports directly onto
    globalThis, so `window.Terminal` IS the class. xterm-addon-fit.min.js's
    wrapper instead assigns the whole module namespace object to
    `window.FitAddon`, so the class is the nested `window.FitAddon.FitAddon`
    — a well-known xterm.js addon UMD quirk. Verified here empirically
    rather than asserted from memory.
    """
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    # Minimal DOM shims: xterm.js's browser-detection helpers touch
    # navigator/document at module-load time, and its UMD header branches on
    # `window` (not `globalThis`) for the plain-<script> case.
    ctx.eval(
        "var window = globalThis;"
        "var navigator = { userAgent: 'py_mini_racer', platform: 'test' };"
        "var document = { documentElement: {} };"
    )
    ctx.eval(XTERM_JS.read_text(encoding="utf-8"))
    ctx.eval(XTERM_FIT.read_text(encoding="utf-8"))

    assert ctx.eval("typeof Terminal") == "function"
    assert ctx.eval("typeof FitAddon") == "object"
    assert ctx.eval("typeof FitAddon.FitAddon") == "function"

    src = _terminal_src()
    assert "new window.Terminal(" in src
    assert "new window.FitAddon.FitAddon(" in src
    # Guard against ever reverting to the (wrong) flat `new window.FitAddon(`.
    assert "new window.FitAddon()" not in src


def test_xterm_registered_in_index_html() -> None:
    text = _index_text()
    assert '<link rel="stylesheet" href="vendor/xterm.min.css" />' in text
    assert '<script src="vendor/xterm.min.js"></script>' in text
    assert '<script src="vendor/xterm-addon-fit.min.js"></script>' in text

    # Plain <script> tags (not text/babel) so they load before _app.js,
    # matching the g6.min.js precedent.
    js_pos = text.index('<script src="vendor/xterm.min.js"></script>')
    fit_pos = text.index('<script src="vendor/xterm-addon-fit.min.js"></script>')
    app_pos = text.index('<script src="_app.js"></script>')
    assert js_pos < app_pos
    assert fit_pos < app_pos
    assert js_pos < fit_pos


def test_studio_terminal_registered_in_babel_bundle_order() -> None:
    order = _babel_order()
    assert "components/studio-terminal.jsx" in order
    assert order.index("components/studio-palette.jsx") < order.index("components/studio-terminal.jsx")
    assert order.index("components/studio-terminal.jsx") < order.index("components/studio.jsx")


def test_vendor_manifest_documents_xterm_with_matching_hashes() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8")
    for name, path in (
        ("xterm.min.js", XTERM_JS),
        ("xterm.min.css", XTERM_CSS),
        ("xterm-addon-fit.min.js", XTERM_FIT),
    ):
        assert name in manifest, f"{name} missing from vendor MANIFEST.md"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest in manifest, f"{name}'s sha256 in MANIFEST.md is stale (got {digest})"
    # A human-reviewable origin URL, not a bare version string.
    assert "unpkg.com/@xterm/xterm" in manifest
    assert "unpkg.com/@xterm/addon-fit" in manifest


def test_terminal_panel_component_and_exports() -> None:
    src = _terminal_src()
    assert "function TerminalPanel(" in src
    assert "function ST_TerminalInstance(" in src
    assert "window.TerminalPanel = TerminalPanel;" in src


def test_terminal_panel_mounted_in_center_column() -> None:
    src = _studio_src()
    center_idx = src.index('data-testid="studio-center"')
    center_block = src[center_idx : center_idx + 800]
    assert "<StudioCenter wid={wid} studio={studio} />" in center_block
    assert "<TerminalPanel wid={wid} studio={studio} />" in center_block
    # Gated on terminalOpen, so unmounting on collapse tears the WS(s) down.
    assert "s.terminalOpen &&" in center_block
    # StudioCenter renders first, terminal panel below it.
    assert center_block.index("<StudioCenter") < center_block.index("<TerminalPanel")


def test_ws_url_and_control_frame_protocol() -> None:
    src = _terminal_src()
    # ws(s)://<host>/v1/workspaces/{wid}/terminal?cols=&rows= — same
    # proto-selection idiom as the chats.jsx chat WS.
    assert 'window.location.protocol === "https:" ? "wss:" : "ws:"' in src
    assert '"/v1/workspaces/" + encodeURIComponent(wid) +' in src
    assert '"/terminal?cols="' in src
    assert '"&rows="' in src

    # Binary frames both directions.
    assert 'ws.binaryType = "arraybuffer"' in src
    assert "encoder.encode(d)" in src, "stdin must be sent as encoded bytes (binary frame), not a text frame"
    assert "new TextEncoder()" in src
    assert "instanceof ArrayBuffer" in src
    assert "term.write(bytes)" in src

    # JSON text-frame control messages both directions.
    assert 'JSON.stringify({ resize: { cols: size.cols, rows: size.rows } })' in src
    assert "msg.exit" in src
    assert "onState(tab.id, \"closed\")" in src


def test_terminal_data_testids_present() -> None:
    src = _terminal_src()
    for testid in (
        'data-testid="terminal-panel"',
        'data-testid="terminal-tab"',
        'data-testid="terminal-new-tab"',
        'data-testid="terminal-body"',
    ):
        assert testid in src, testid
    # Dynamic conn-state testid: terminal-conn-<connecting|live|closed|error>.
    assert 'data-testid={"terminal-conn-" + st}' in src
    for state in ("connecting", "live", "closed", "error"):
        assert re.search(rf'\b{state}\b', src), state


def test_terminal_lifecycle_cleanup_present() -> None:
    src = _terminal_src()
    # Teardown on unmount (tab close / panel collapse / wid change via key).
    assert "term.dispose();" in src
    assert "wsRef.current.close();" in src
    assert 'key={wid + ":" + tab.id}' in src, "wid must be part of the key so a workspace switch remounts (tears down) every tab"
    # Fit wiring: mount, active-tab switch, and debounced resize (ResizeObserver + window resize).
    assert "fitAddon.fit();" in src
    assert "ResizeObserver" in src
    assert 'window.addEventListener("resize", scheduleFit);' in src


def test_css_term_classes_and_mobile_hide_present() -> None:
    css = STYLES.read_text(encoding="utf-8")
    for cls in (".st-term-panel", ".st-term-head", ".st-term-tab", ".st-term-body", ".st-term-conn"):
        assert cls in css, cls
    # Hidden on phones for v1 (structural note, not a functional gate).
    mobile_start = css.index("@media (max-width: 639px)")
    mobile_end = css.index("\n}\n", mobile_start)
    assert ".st-term-panel { display: none; }" in css[mobile_start:mobile_end]


def test_bundle_transpiles_with_terminal() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio-terminal.jsx === */" in text
