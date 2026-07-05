"""Static JSX checks — chats.jsx loads the tail of history first and
lazy-loads older messages on scroll-up.

Behavioural contract:

* Initial REST round-trip targets ``?before_seq=<sentinel>`` (not the
  old ``after_seq=0`` full-history loop).
* The WebSocket opens with ``cursor=<lastSeq>`` so it only streams
  NEW frames after the tail fetch lands.
* The scroll container reacts to near-top scroll position by paging
  in older messages via ``before_seq=<oldestSeq>``.
* Auto-scroll-to-bottom is gated on tail growth (``lastSeq``), not
  generic ``messages`` changes, so prepends don't yank the user away
  from older content they're reading.
"""

from __future__ import annotations

from pathlib import Path


CHATS_JSX = Path(__file__).resolve().parents[2] / "ui" / "components" / "chats.jsx"
# Task B2 (chat-refactor plan) moved the WS/data lifecycle + tail-load
# wholesale out of ChatDetail (chats.jsx) into the embeddable
# <Conversation> core — read both files so these behavioral-contract
# assertions keep holding regardless of which file the logic lives in.
CONVERSATION_JSX = (
    Path(__file__).resolve().parents[2] / "ui" / "components" / "chat" / "conversation.jsx"
)


def _src() -> str:
    return (
        CHATS_JSX.read_text(encoding="utf-8")
        + "\n"
        + CONVERSATION_JSX.read_text(encoding="utf-8")
    )


def test_initial_fetch_uses_before_seq_sentinel() -> None:
    src = _src()
    # Tail-load loop walks pages backwards using ?before_seq=${cursor}
    # where the initial cursor is the SENTINEL_TAIL_SEQ; the sentinel
    # constant itself stays unchanged.
    assert "SENTINEL_TAIL_SEQ" in src, "tail sentinel constant must be defined"
    assert "before_seq=${cursor}" in src, (
        "initial REST load must page backwards using before_seq=${cursor}"
    )
    assert "cursor = SENTINEL_TAIL_SEQ" in src, (
        "loop must start the cursor at SENTINEL_TAIL_SEQ"
    )
    assert "Number.MAX_SAFE_INTEGER" in src, (
        "SENTINEL_TAIL_SEQ must be defined from MAX_SAFE_INTEGER"
    )


def test_old_full_history_loop_is_gone() -> None:
    src = _src()
    # Old after_seq=0 / after_seq=${cursor} paged-from-the-start loop
    # must not return — we always page BACKWARDS from the tail now.
    assert "after_seq=0" not in src, (
        "after_seq=0 paginated full-history loop must not return"
    )
    assert "after_seq=${cursor}" not in src


def test_websocket_uses_lastseq_cursor() -> None:
    src = _src()
    # The WS must open with the tail's last seq as cursor. The reconnect
    # implementation tracks a `latestSeq` variable (initialised to
    # initialLoadedSeq) so both the first connect and any reconnect
    # resume from the correct position.
    has_cursor = (
        "ws?cursor=${initialLoadedSeq}" in src
        or "ws?cursor=${latestSeq}" in src
    )
    assert has_cursor, (
        "WS URL must carry cursor=${initialLoadedSeq} (or cursor=${latestSeq} "
        "when the backoff-reconnect pattern is used) so the server only "
        "streams NEW frames after the tail fetch lands"
    )
    assert "initialLoadedSeq == null" in src or "initialLoadedSeq != null" in src, (
        "WS open must be gated on the initial REST load completing"
    )
    # Reconnect variant: latestSeq must be initialised to initialLoadedSeq.
    if "latestSeq" in src:
        assert "latestSeq = initialLoadedSeq" in src, (
            "latestSeq must be seeded from initialLoadedSeq so the first "
            "connect uses the tail's highest seq as the cursor"
        )


def test_load_older_paginates_via_before_seq() -> None:
    src = _src()
    assert "loadOlder" in src
    assert "before_seq=${oldestSeq}" in src, (
        "scroll-up pagination must use the oldest loaded seq as the cursor"
    )
    # The scroll handler should trigger the older-page fetch when near
    # the top of the container.
    assert "el.scrollTop < 100" in src


def test_autoscroll_gated_on_lastseq() -> None:
    src = _src()
    # The dependency array on the auto-scroll effect must key off
    # lastSeq (monotone tail growth), not the entire messages array,
    # so prepends don't trigger a scroll-to-bottom.
    assert "}, [lastSeq, waitingForReply]);" in src, (
        "auto-scroll effect must depend on [lastSeq, waitingForReply]"
    )


def test_scroll_position_preserved_on_prepend() -> None:
    src = _src()
    # After prepending older rows, the scroll position must be
    # restored so the user keeps reading from where they were.
    assert "oldScrollHeight" in src
    assert "oldScrollTop" in src
