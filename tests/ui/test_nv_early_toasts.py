"""Toasts pushed before NV_ToastHost mounts are lost (and so is the `text`
they were pushed with).

``ui/app.jsx`` routes window "error" / "unhandledrejection" to
``primerApi.toastPush({kind: "error", text})``. Before the shell mounts that
lands in the foundation queue (``ui/foundation/toast.js``, loaded first).
``NV_ToastHost`` takes over ``primerApi.toastPush`` on mount but starts from
an empty state and never reads what the queue already holds, and the queue's
``push()`` builds its entry from a fixed field list that drops ``text``. So a
boot-time or auth-screen error is never displayed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._support.js_source import blank, close_of
from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TOAST_JS = (UI / "foundation" / "toast.js").read_text(encoding="utf-8")
SHELL_PATH = UI / "components" / "console" / "nv-shell.jsx"


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _function(code: str, name: str) -> str:
    """One top-level ``function <name>(...) {...}`` of ``code``."""
    start = code.index(f"function {name}(")
    blanked = blank(code, strings=True, templates=True)
    body = blanked.index("{", blanked.index(")", start))
    end = close_of(blanked, body)
    assert end > 0, name
    return code[start:end]


# ---- (a) the foundation queue keeps the caller's fields ----------------------


def test_foundation_push_keeps_the_caller_text() -> None:
    """app.jsx's root reporter pushes {kind: "error", text}; the queue entry
    must keep ``text``, or an early toast renders blank even if it ever shows."""
    ctx = mini_react_context(TOAST_JS)
    try:
        ctx.eval('window.primerApi.toastPush({ kind: "error", text: "boom", requestId: "r-1" })')
        entry = js(ctx, "window.primerApi._toast.state.toasts[0]")
    finally:
        ctx.close()
    assert entry["kind"] == "error"
    assert entry["requestId"] == "r-1"
    assert entry["text"] == "boom", (
        "the foundation queue dropped the caller's `text` field: a toast "
        "pushed before the host mounts carries no message"
    )


# ---- (b) the host drains the queue when it mounts -----------------------------


@pytest.fixture(scope="module")
def shell_transpiled() -> str:
    return transpile(SHELL_PATH)


def test_the_toast_host_drains_toasts_queued_before_it_mounts(shell_transpiled) -> None:
    """A boot-time failure lands in the foundation queue before the shell
    mounts; mounting the host must render it through the live regions and
    leave the queue empty (the foundation's timers die with their entries)."""
    prelude = "var window = globalThis;\n" + TOAST_JS
    ctx = mini_react_context(_function(shell_transpiled, "NV_ToastHost"), prelude)
    try:
        ctx.eval('window.primerApi.toastPush({ kind: "error", text: "boot failed" })')
        assert js(ctx, "window.primerApi._toast.state.toasts.length") == 1
        ctx.eval("MR.mount(NV_ToastHost, {})")
        texts = js(ctx, "MR.texts()")
        queue = js(ctx, "window.primerApi._toast.state.toasts")
    finally:
        ctx.close()
    assert any("boot failed" in t for t in texts), (
        "the toast pushed before the host mounted was never rendered: the "
        "host takes over primerApi.toastPush on mount but starts from an "
        "empty state and never reads the foundation queue"
    )
    assert queue == [], (
        "the foundation queue still holds the replayed toast: the host must "
        "drain it on mount (dismiss each entry so its timer is cleared), or "
        "the queue and the rendered stack disagree"
    )
