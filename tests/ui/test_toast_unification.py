"""FB2 regression guard — mutation error toasts must be visible.

use-mutation.js falls back to window.primerApi.toastPush (the toast.js queue)
when a caller omits onError. Nothing rendered that queue — the visible toast
stack in app.jsx is a separate local state fed only by app.jsx's pushToast. So
mutations without an explicit onError rolled back silently.

The fix points window.primerApi.toastPush at app.jsx's rendered `pushToast` so
those default error toasts surface. These checks assert both halves of the wire.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
APP = UI / "app.jsx"
USE_MUTATION = UI / "foundation" / "use-mutation.js"


def test_use_mutation_still_falls_back_to_toast_push() -> None:
    src = USE_MUTATION.read_text(encoding="utf-8")
    # The default-error path uses the shared global entry point.
    assert "ns.toastPush" in src


def test_app_points_global_toast_push_at_rendered_stack() -> None:
    src = APP.read_text(encoding="utf-8")
    # app.jsx overrides the global toastPush so it feeds the rendered stack.
    assert "api.toastPush = " in src
    # …and it ultimately routes to app's pushToast (via the stable ref wrapper).
    assert "pushToastRef.current" in src
    assert "pushToastRef = React.useRef(pushToast)" in src


def test_app_also_wires_global_toast_dismiss() -> None:
    src = APP.read_text(encoding="utf-8")
    assert "api.toastDismiss = " in src
    assert "removeToastRef.current" in src


def test_bundle_transpiles_with_toast_wiring() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
