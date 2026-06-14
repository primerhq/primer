"""Static JSX / source checks for the 4 UI-polish fixes.

Fix 1 - window.MOCK confinement
    app.jsx must not initialise sessions/workers from window.MOCK; it
    must not use window.MOCK.AGENTS.length in any rendered subtitle; the
    sessions-page header must derive its count from the live API
    (counts.sessions) not from mock array length.

Fix 2 - WebSocket exponential-backoff reconnect
    chats.jsx and session-detail.jsx must reconnect on unexpected close
    with exponential backoff (MAX_BACKOFF_MS cap), resume from the last
    received seq (latestSeq / initialLoadedSeq cursor), and NOT reconnect
    on intentional unmount close.

Fix 3 - Stale "executor not yet shipped" copy removed
    app.jsx graphs page subtitle must not contain the stale placeholder.

Fix 4 - Composer clear only on successful send
    chats.jsx sendMessage must return a boolean; onSubmitComposer must
    gate setComposer("") on that return value so a failed send (WS not
    open) leaves the user's text intact.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
APP = UI / "app.jsx"
CHATS = UI / "components" / "chats.jsx"
SDET = UI / "components" / "session-detail.jsx"


def _app() -> str:
    return APP.read_text(encoding="utf-8")


def _chats() -> str:
    return CHATS.read_text(encoding="utf-8")


def _sdet() -> str:
    return SDET.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix 1: window.MOCK confinement
# ---------------------------------------------------------------------------


def test_app_sessions_not_from_mock() -> None:
    src = _app()
    # The initializer pattern (not a comment) must be gone.
    assert "useState(() => window.MOCK.buildSessions" not in src, (
        "app.jsx must not initialise sessions state from window.MOCK.buildSessions; "
        "page components (sessions-list.jsx, session-detail.jsx) fetch from the live API"
    )


def test_app_workers_not_from_mock() -> None:
    src = _app()
    # The initializer pattern (not a comment) must be gone.
    assert "useState(window.MOCK.WORKERS)" not in src, (
        "app.jsx must not initialise workers state from window.MOCK.WORKERS; "
        "workers.jsx fetches /v1/workers directly"
    )


def test_app_agents_subtitle_not_from_mock() -> None:
    src = _app()
    assert "window.MOCK.AGENTS.length" not in src, (
        "agents-page subtitle in app.jsx must not read window.MOCK.AGENTS.length; "
        "use a static description -- the real count lives inside AgentsPage"
    )


def test_app_sessions_page_header_uses_real_count() -> None:
    src = _app()
    # The sessions-page subtitle must use counts.sessions (live API total),
    # not sessions.length (was mock array) or sessions.filter (was mock live count).
    assert "counts.sessions" in src, (
        "sessions-page header must derive total from counts.sessions (live API), "
        "not the stale mock sessions array"
    )
    # The mock-derived expressions must be gone.
    assert "sessions.length" not in src, (
        "sessions.length in app.jsx header must be removed -- was reading mock data"
    )
    assert "sessions.filter" not in src, (
        "sessions.filter in app.jsx header must be removed -- was reading mock data"
    )


# ---------------------------------------------------------------------------
# Fix 2: WebSocket exponential-backoff reconnect
# ---------------------------------------------------------------------------


def test_chats_ws_backoff_constants() -> None:
    src = _chats()
    assert "MAX_BACKOFF_MS" in src, "chats.jsx WS effect must define MAX_BACKOFF_MS cap"
    assert "backoffMs" in src, "chats.jsx WS effect must use a backoffMs variable"


def test_chats_ws_reconnect_on_unexpected_close() -> None:
    src = _chats()
    assert "reconnectTimer" in src, (
        "chats.jsx must schedule reconnect via a timer on unexpected close"
    )
    assert "Math.min(backoffMs * 2" in src or "Math.min(backoffMs*2" in src, (
        "chats.jsx backoff must double each attempt and cap at MAX_BACKOFF_MS"
    )


def test_chats_ws_no_reconnect_on_intentional_close() -> None:
    src = _chats()
    assert "intentional" in src, (
        "chats.jsx must use an intentional flag to skip reconnect on unmount"
    )
    assert "clearTimeout(reconnectTimer)" in src, (
        "chats.jsx cleanup must cancel any pending reconnect timer"
    )


def test_chats_ws_resume_from_latest_seq() -> None:
    src = _chats()
    assert "latestSeq" in src, (
        "chats.jsx must track latestSeq in the WS effect closure and use it "
        "as the reconnect cursor so no frames are missed"
    )
    assert "cursor=${latestSeq}" in src, (
        "chats.jsx WS URL must use cursor=${latestSeq} for reconnects"
    )


def test_sdet_ws_backoff_constants() -> None:
    src = _sdet()
    assert "MAX_BACKOFF_MS" in src, "session-detail.jsx WS effect must define MAX_BACKOFF_MS cap"
    assert "backoffMs" in src, "session-detail.jsx WS effect must use a backoffMs variable"


def test_sdet_ws_reconnect_on_unexpected_close() -> None:
    src = _sdet()
    assert "reconnectTimer" in src, (
        "session-detail.jsx must schedule reconnect via a timer on unexpected close"
    )


def test_sdet_ws_no_reconnect_on_intentional_close() -> None:
    src = _sdet()
    assert "intentional" in src, (
        "session-detail.jsx must use an intentional flag to skip reconnect on unmount"
    )
    assert "clearTimeout(reconnectTimer)" in src, (
        "session-detail.jsx cleanup must cancel any pending reconnect timer"
    )


def test_sdet_ws_resume_from_latest_seq() -> None:
    src = _sdet()
    assert "latestSeq" in src, (
        "session-detail.jsx must track latestSeq and resume from it on reconnect"
    )
    assert "cursor=${latestSeq}" in src, (
        "session-detail.jsx WS URL must use cursor=${latestSeq} for reconnects"
    )


# ---------------------------------------------------------------------------
# Fix 3: Stale "executor not yet shipped" copy removed
# ---------------------------------------------------------------------------


def test_graphs_page_no_stale_executor_copy() -> None:
    src = _app()
    assert "executor not yet shipped" not in src, (
        "graphs-page subtitle must not say 'executor not yet shipped' -- "
        "the graph executor (primer/graph/executor.py) is fully implemented"
    )


def test_graphs_page_has_accurate_subtitle() -> None:
    src = _app()
    # Verify an accurate description is present near the graphs page section.
    assert "Multi-agent flows" in src, (
        "graphs page subtitle should still identify the section as multi-agent flows"
    )


# ---------------------------------------------------------------------------
# Fix 4: Composer clear only on successful send
# ---------------------------------------------------------------------------


def test_chats_send_message_returns_bool() -> None:
    src = _chats()
    assert "return false" in src, (
        "sendMessage must return false when the WS is not open "
        "so onSubmitComposer can preserve the composer text"
    )
    assert "return true" in src, (
        "sendMessage must return true on a successful enqueue"
    )


def test_chats_composer_clear_gated_on_send_success() -> None:
    src = _chats()
    # The composer must only be cleared after a successful send.
    # sendMessage is assigned to `sent`; setComposer("") must appear
    # inside the if(sent) branch, not unconditionally after the call.
    assert 'const sent = sendMessage(' in src, (
        "onSubmitComposer must capture the return value of sendMessage"
    )
    assert 'if (sent)' in src, (
        "setComposer('') and setAttachments([]) must be guarded by if (sent)"
    )
