"""The destroy tab's session count says it is loading while its read is out (UI-L9e).

``WS_DestroyTab`` (``ui/components/workspaces.jsx``) derived its "active sessions" and
"total sessions" cells from the badge read's ``items ?? []``, so while that paged read was
still loading the cells showed "0 active sessions" - a count that reads as "nothing to
cancel" before the destroy confirm. The cells now show a loading state until the read lands.

MiniRacer on the real file with a stubbed ``primerApi`` and the ``sessionsForBadge`` snapshot
the test hands the tab (loading, then landed).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
WORKSPACES = ROOT / "ui" / "components" / "workspaces.jsx"

PRELUDE = r"""
var window = globalThis;
globalThis.Btn = function (props) {
  return React.createElement("button", { "data-testid": props["data-testid"] || "ws-btn", onClick: props.onClick });
};
globalThis.Banner = function (props) {
  return React.createElement("div", { "data-testid": "ws-banner" }, props.title, props.detail, props.children);
};
globalThis.Modal = function (props) {
  return props.children ? React.createElement("div", { "data-testid": "ws-modal" }, props.children) : null;
};
window.primerApi = {
  useMutation: function () {
    return { loading: false, mutate: function () { return Promise.resolve(); } };
  },
  useRouter: function () { return { navigate: function () {} }; },
  apiFetch: function () { return Promise.resolve({}); },
};
"""

SNAP_LOADING = {"data": None, "loading": True, "degraded": False, "error": None}
SNAP_READY = {
    "data": {"items": [{"id": "s1", "status": "running"}, {"id": "s2", "status": "ended"}]},
    "loading": False, "degraded": False, "error": None,
}


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _cell(ctx, testid: str):
    # the harness holds a single JSX child as the value itself, not a one-element array.
    return _js(
        ctx,
        f"(function () {{ var el = MR.find('{testid}'); if (!el) return null;"
        " var c = el.props.children; return Array.isArray(c) ? c[0] : c; })()",
    )


def _ctx(snap):
    c = mini_react_context(transpile(WORKSPACES), PRELUDE)
    c.eval(
        "MR.mount(WS_DestroyTab, { wid: 'w1', pushToast: function () {}, sessionsForBadge: "
        + json.dumps(snap)
        + " });"
    )
    return c


def test_the_count_cells_show_loading_while_the_read_is_out() -> None:
    # Red (UI-L9e): the badge read has not landed; the cells must not say "0".
    ctx = _ctx(SNAP_LOADING)
    try:
        assert _cell(ctx, "ws-destroy-active") == "…"
        assert _cell(ctx, "ws-destroy-total") == "…"
    finally:
        ctx.close()


def test_the_count_cells_show_the_landed_counts() -> None:
    # Characterisation: the read lands (one running, one ended): one active of two,
    # with the "will be cancelled" hint on the active cell.
    ctx = _ctx(SNAP_READY)
    try:
        texts = _js(ctx, "MR.texts()")
        assert "1" in texts and "2" in texts, "the landed counts are not on screen"
        assert "(will be cancelled)" in texts
    finally:
        ctx.close()


def test_a_landing_read_after_the_loading_state_swaps_in_the_counts() -> None:
    # Characterisation: the tab starts loading and the read lands; the counts
    # swap in on the re-render.
    ctx = _ctx(SNAP_LOADING)
    try:
        ctx.eval(
            "MR.rerender({ wid: 'w1', pushToast: function () {}, sessionsForBadge: "
            + json.dumps(SNAP_READY)
            + " });"
        )
        texts = _js(ctx, "MR.texts()")
        assert "1" in texts and "2" in texts, "the landed counts did not swap in"
        assert "0" not in texts, "the pre-landing 0 is still on screen after the read landed"
    finally:
        ctx.close()
