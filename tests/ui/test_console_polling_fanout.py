"""T87 (UI-L5, UI-L10, UI-L11, UI-L15, GU-9): the console's tap refetches keep their trailing frame, one endpoint is read under one key, the approvals audit
does not poll once per parked session at the bare 5 s, and the agents and graphs pages cap their per-row reads."""

from __future__ import annotations

import json
import re
from pathlib import Path

from py_mini_racer import MiniRacer

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CONSOLE = UI / "components" / "console"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _api_ctx() -> MiniRacer:
    ctx = MiniRacer()
    ctx.eval(
        "var window = globalThis; window.React = {};"
        "var TIMERS = []; var NOW = 0;"
        "function setTimeout(fn, ms) { TIMERS.push({fn: fn, at: NOW + ms}); return TIMERS.length; }"
        "function clearTimeout(id) {}"
        "function advance(ms) { NOW += ms; var due = TIMERS.filter(function (t) { return t.at <= NOW; });"
        " TIMERS = TIMERS.filter(function (t) { return t.at > NOW; }); due.forEach(function (t) { t.fn(); }); }"
    )
    ctx.eval(_read(UI / "foundation" / "api.js"))
    return ctx


def test_a_frame_inside_the_window_gets_one_trailing_refetch() -> None:
    """UI-L5 / GU-9: two frames 100 ms apart are two refetches, not one."""
    ctx = _api_ctx()
    ctx.eval("var state = {}; var n = 0; function go() { PR_trailingThrottle(state, 500, function () { n += 1; }); }")
    ctx.eval("go();")
    assert ctx.eval("n") == 1, "the first frame refetches at once"
    ctx.eval("advance(100); go();")
    assert ctx.eval("n") == 1, "the second waits for the window"
    ctx.eval("go(); advance(400);")
    assert ctx.eval("n") == 2, "one trailing refetch for every frame inside the window"
    ctx.eval("advance(600);")
    assert ctx.eval("n") == 2, "a quiet window does not refetch again"
    ctx.eval("go();")
    assert ctx.eval("n") == 3


def test_map_limit_runs_at_most_four_at_once_and_keeps_order() -> None:
    ctx = _api_ctx()
    ctx.eval(
        "var live = 0, peak = 0; var out = null;"
        "var items = [0,1,2,3,4,5,6,7,8,9];"
        "PR_mapLimit(items, 4, function (i) { live += 1; peak = Math.max(peak, live);"
        " return new Promise(function (r) { Promise.resolve().then(function () { live -= 1; r(i * 2); }); }); })"
        ".then(function (v) { out = v; });"
    )
    ctx.eval("Promise.resolve();")
    assert ctx.eval("peak") <= 4
    assert json.loads(ctx.eval("JSON.stringify(out)")) == [i * 2 for i in range(10)]


def test_the_three_trailing_listeners_use_the_shared_throttle() -> None:
    for name in ("nv-rail.jsx", "nv-mobile-shell.jsx"):
        src = _read(CONSOLE / name)
        assert "PR_trailingThrottle(" in src, name
        assert "if (attentionRefetchTimerRef.current) return;" not in src
        assert "if (refetchTimerRef.current) return;" not in src


def test_pending_attention_is_read_under_one_key() -> None:
    """UI-L11: the rail, the system health card, the system attention panel and both mobile tabs share one raw read."""
    hits = 0
    for path in list(CONSOLE.glob("*.jsx")) + [UI / "components" / "shell" / "sh-api.jsx"]:
        for line in _read(path).splitlines():
            if "pendingAttention(" in line and not line.strip().startswith(("//", "pendingAttention:")):
                hits += 1
    assert hits == 1, "only SH_useAttention reads the aggregate"
    assert "function SH_useAttention(" in _read(UI / "components" / "shell" / "sh-api.jsx")
    for name in ("nv-rail.jsx", "nv-system.jsx", "nv-mobile-shell.jsx"):
        assert "SH_useAttention(" in _read(CONSOLE / name), name


def test_the_audit_does_not_poll_once_per_session_at_the_bare_five_seconds() -> None:
    """UI-L10."""
    src = _read(CONSOLE / "nv-platform.jsx")
    fetcher = src[src.index("function NV_AuditPendingFetcher"):src.index("var NV_AUDIT_PAGE_SIZE")]
    assert "pollMs: 5000" not in fetcher and "PR_pagedPollMs(5000" in fetcher
    audit = src[src.index("function NV_ApprovalsAudit"):]
    assert "PR_fetchAllFind(" in audit and "length: 100" not in audit.split("var records")[0]
    assert "useWorkspaceTapListener" in audit


def test_the_agent_sessions_panel_reads_one_page_of_ten() -> None:
    """UI-L15."""
    src = _read(UI / "components" / "agents.jsx")
    panel = src[src.index("function AG_SessionsPanel"):]
    panel = panel[: panel.index("\n}\n")]
    assert "limit=10" in panel and "PR_fetchAllPages" not in panel and "PR_usePagedResource" not in panel


def test_the_list_pages_cap_their_per_row_reads() -> None:
    for name in ("agents.jsx", "graphs.jsx"):
        src = _read(UI / "components" / name)
        assert "PR_mapLimit(" in src, name
        assert re.search(r"Promise\.all\(\s*items\.map", src) is None, name
