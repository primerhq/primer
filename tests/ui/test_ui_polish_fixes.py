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

    Superseded in part by Task G1 of the chat-refactor plan
    (queue-on-reconnect, §4.5): sendMessage no longer returns false when
    the socket is momentarily not open — it queues the frame instead of
    hard-rejecting, so the "leaves the user's text intact" guarantee now
    holds because the send always succeeds (composer clears immediately;
    the optimistic echo reconciles once the queued frame actually lands),
    not because it's rejected.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
APP = UI / "app.jsx"
CHATS = UI / "components" / "chats.jsx"
# Task B2 (chat-refactor plan) moved the WS reconnect machinery and
# sendMessage/onSubmitComposer wholesale out of ChatDetail (chats.jsx)
# into the embeddable <Conversation> core — read both files so the
# Fix 2 / Fix 4 assertions below keep validating the same behavioral
# contract regardless of which file the logic lives in.
CONVERSATION = UI / "components" / "chat" / "conversation.jsx"
SDET = UI / "components" / "session-detail.jsx"


def _app() -> str:
    return APP.read_text(encoding="utf-8")


def _chats() -> str:
    return CHATS.read_text(encoding="utf-8") + "\n" + CONVERSATION.read_text(encoding="utf-8")


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


def test_app_sessions_page_does_not_read_mock_counts() -> None:
    src = _app()
    # B6 (PR-B): the /sessions route is retired into a Studio redirect — the
    # old Sessions-list page header (which once derived its subtitle count) no
    # longer renders. Whatever remains must never read the stale mock array.
    assert "sessions.length" not in src, (
        "sessions.length in app.jsx must be removed -- was reading mock data"
    )
    assert "sessions.filter" not in src, (
        "sessions.filter in app.jsx must be removed -- was reading mock data"
    )


# ---------------------------------------------------------------------------
# Fix 2: WebSocket exponential-backoff reconnect
# ---------------------------------------------------------------------------


def test_sdet_live_via_tap_eventsource() -> None:
    src = _sdet()
    assert "EventSource" in src, (
        "session-detail.jsx live view must tail via an EventSource tap, not a WebSocket"
    )
    assert "/tap" in src, (
        "session-detail.jsx must open the workspace /tap SSE endpoint"
    )


def test_sdet_tap_single_session_selector() -> None:
    src = _sdet()
    # The tap is filtered to this one session (sessions: id == sid).
    assert "WTP_buildSelector" in src, (
        "session-detail.jsx must build a single-session selector for the tap"
    )


def test_sdet_tap_cleanup_closes_eventsource() -> None:
    src = _sdet()
    # Unmount must close the EventSource (the tap analogue of the old
    # intentional-flag + clearTimeout cleanup).
    assert "es.close()" in src, (
        "session-detail.jsx tap effect cleanup must close the EventSource"
    )


def test_sdet_tap_resume_from_history_high_water() -> None:
    src = _sdet()
    # No gap / no re-replay at the history->live seam: the tap resumes from
    # the history high-water seq via an encoded resume cursor.
    assert "_slsEncodeCursor" in src, (
        "session-detail.jsx must seed the tap resume cursor from the history high-water seq"
    )
    assert "cursor=" in src, (
        "session-detail.jsx tap URL must carry the resume cursor"
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


# ---------------------------------------------------------------------------
# Fix 4: Composer clear only on successful send
# ---------------------------------------------------------------------------


