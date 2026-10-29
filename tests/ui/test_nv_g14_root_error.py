"""G14-01 (HIGH): the console document has no root error boundary and no
global error/rejection handler, and it ships a PRODUCTION React build.

``ui/app.jsx`` renders ``window.AuthGate > window.NV_Shell`` straight into
``createRoot`` with no wrapper, the ONLY ErrorBoundary in non-vendor ``ui/``
is the graph builder's scoped ``GB_ErrorBoundary`` (it wraps the builder's
content only), and the ONLY window error/unhandledrejection handlers live
in the canvas frame's own postMessage-bridged document. On a production
React build (the vendored ``react.production`` marker) a render crash on
any console surface outside the builder therefore unmounts the whole tree
(blank #root, reload required, URL-held selection and any in-memory draft
lost), and an unhandled promise rejection in the main document is
invisible (a JS-console warning only).

Audit: /var/tmp/revamp/audit/G14.md (G14-01 row); the auditor's probe
(/var/tmp/revamp/audit/G14/probe_g14_01_no_root_error_boundary.py) pins the
RED state, these tests pin the GREEN state, anchored on the real render
line (the createRoot mount) and the real registration lines (the
window.addEventListener calls), not a paraphrase.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "ui" / "app.jsx").read_text(encoding="utf-8")
BUILDER = (ROOT / "ui" / "components" / "graph-builder" / "graph-builder.jsx").read_text(encoding="utf-8")
CANVAS = (ROOT / "ui" / "canvas" / "html-bootstrap.js").read_text(encoding="utf-8")
VENDOR = (ROOT / "ui" / "vendor" / "react.min.js").read_text(encoding="utf-8")

# The console document's single mount point (the real render line).
RENDER_LINE = re.compile(r"ReactDOM\.createRoot\(document\.getElementById\(\"root\"\)\)\.render\(")
# The post-fix render line: the mount wraps <AppRoot /> in a boundary element.
RENDER_WRAPPED = re.compile(
    r"ReactDOM\.createRoot\(document\.getElementById\(\"root\"\)\)\.render\(\s*"
    r"<([A-Za-z_]\w*)>\s*<AppRoot\s*/>\s*</\1\s*>"
)


def _non_vendor_ui_sources() -> dict[str, str]:
    """Every non-vendor .js/.jsx source under ui/ (where a console-document
    boundary or global handler could live), keyed by repo-relative path."""
    sources: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(ROOT / "ui"):
        dirnames[:] = [d for d in dirnames if d != "vendor"]
        for fn in filenames:
            if fn.endswith((".jsx", ".js")):
                rel = os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(os.sep, "/")
                sources[rel] = (ROOT / rel).read_text(encoding="utf-8")
    return sources


def _boundary_body(name: str) -> str:
    """The named boundary class's body in app.jsx, from the class head to its
    column-0 close (the house class shape, cf. GB_ErrorBoundary)."""
    head = re.search(r"class\s+" + re.escape(name) + r"\s+extends\s+React\.Component\s*\{", APP)
    if not head:
        return ""
    return APP[head.start() : APP.index("\n}", head.start()) + 1]


# ---- anchors (hold at the base AND after the fix; the fix lands around them) ----


def test_the_fix_lands_around_lines_it_does_not_move() -> None:
    """The real render line, the AppRoot shape, the production vendor marker,
    the graph builder's scoped boundary, and the canvas frame's own-document
    bridge hold at the base and after the fix: the root boundary is a BACKSTOP
    above the builder's (which keeps its in-place Undo/Discard/Clear/Retry
    recovery) and the canvas bridge stays in its own postMessage document."""
    assert RENDER_LINE.search(APP), "app.jsx's createRoot mount line moved"
    assert "<window.AuthGate>" in APP and "<window.NV_Shell" in APP, (
        "the AppRoot shape (AuthGate > NV_Shell) moved"
    )
    assert "react.production" in VENDOR, (
        "the vendored React production marker is gone (check the build)"
    )
    assert "class GB_ErrorBoundary extends React.Component" in BUILDER, (
        "the graph builder's scoped GB_ErrorBoundary moved"
    )
    assert re.search(r'window\.addEventListener\(\s*"unhandledrejection"', CANVAS), (
        "the canvas frame's own-document error bridge moved"
    )


# ---- the fix: a root boundary at the real render line ---------------------------


def test_the_root_boundary_wraps_app_root_at_the_render_line() -> None:
    """The console's root renders <AppRoot /> through a root error boundary at
    the real createRoot(...).render(...) line, so a render crash on any
    surface outside the builder is caught instead of unmounting the
    production tree. House shape (graph-builder.jsx:12): a React.Component
    subclass with getDerivedStateFromError + componentDidCatch + a fallback
    that says a screen crashed, shows the error message, and offers a full
    reload (window.location.reload - the honest recovery at the root, where
    the builder's in-place Undo/Discard does not apply)."""
    m = RENDER_WRAPPED.search(APP)
    assert m, (
        "app.jsx renders <AppRoot /> straight into createRoot with no root "
        "error boundary (G14-01): a render crash outside the graph builder "
        "unmounts the whole production tree to a blank page"
    )
    body = _boundary_body(m.group(1))
    assert body, (
        f"the render line's wrapper {m.group(1)!r} is not a React.Component "
        "subclass defined in app.jsx"
    )
    for member in ("getDerivedStateFromError", "componentDidCatch"):
        assert member in body, f"the root boundary lacks the house member {member}"
    assert "window.location.reload" in body, (
        "the root boundary's fallback must offer a full reload "
        "(window.location.reload): there is no in-place recovery at the root"
    )
    assert ".message" in body and "String(" in body, (
        "the root boundary's fallback must show the error message (house "
        "detail line: error.message, falling back to String(error))"
    )


# ---- the fix: global handlers in the console document ---------------------------


def test_the_console_document_installs_global_handlers_outside_the_canvas_frame() -> None:
    """A window "error" handler AND a window "unhandledrejection" handler
    registered on the real addEventListener lines by a source in the CONSOLE
    document (outside ui/canvas/), so an unhandled rejection in the main
    document is no longer a JS-console-only warning. The canvas frame's
    handlers are a separate postMessage-bridged document and do not protect
    the console document."""
    sources = _non_vendor_ui_sources()
    files = [
        rel
        for rel, src in sources.items()
        if not rel.startswith("ui/canvas/")
        and re.search(r'window\.addEventListener\(\s*"error"', src)
        and re.search(r'window\.addEventListener\(\s*"unhandledrejection"', src)
    ]
    assert files, (
        "no console-document source installs window 'error' + "
        "'unhandledrejection' handlers (only the canvas frame's own document "
        "does): an unhandled rejection in the main document is invisible "
        "(G14-01)"
    )


def test_the_handlers_route_to_the_toast_host_as_error_toasts() -> None:
    """The rejection reason is handed to the console's one toast stack -
    primerApi.toastPush (the seam NV_ToastHost takes over while mounted) -
    as kind "error", so the user sees it. No double-report: a render crash
    is caught by React at the boundary (no window event fires for it), so
    the window handlers only see uncaught script errors and unhandled
    rejections - disjoint from the boundary's path."""
    sources = _non_vendor_ui_sources()
    files = [
        rel
        for rel, src in sources.items()
        if not rel.startswith("ui/canvas/")
        and re.search(r'window\.addEventListener\(\s*"unhandledrejection"', src)
        and "toastPush" in src
        and 'kind: "error"' in src
    ]
    assert files, (
        "the console-document unhandledrejection handler does not route to "
        "the existing toast host (primerApi.toastPush, kind 'error')"
    )
