"""Structural checks for the WorkspaceTap history backfill (Part C).

The workspace tap connects live-from-now, so the activity stream shows nothing
for events that happened before it opened. WorkspaceTap now fetches a bounded
history snapshot on mount (GET /v1/workspaces/{wid}/events) and merges it below
the live tail, deduped by (session_id, seq). Static-source checks only.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TAP = UI / "components" / "workspace-tap.jsx"


def _tap_src() -> str:
    return TAP.read_text(encoding="utf-8")


def test_history_backfill_fetch_on_mount() -> None:
    src = _tap_src()
    # Fetches the bounded workspace events history via apiFetch on mount.
    assert "window.primerApi.apiFetch" in src
    assert '"/workspaces/" + encodeURIComponent(wid) + "/events?limit=200"' in src
    # Held in local state seeded from the response items.
    assert "setHistory" in src


def test_history_merged_and_deduped_by_session_seq() -> None:
    src = _tap_src()
    # Stable seam key is (session_id, seq), NOT the per-frame cursor.
    assert "function WTP_eventKey(" in src
    # History + live are merged into one buffer.
    assert "history" in src and "liveEvents" in src
    # Deduped (first-seen wins) and time-ordered so history sorts above live.
    assert "seen[k]" in src
    assert "Date.parse(a.ts)" in src


def test_clear_also_clears_history() -> None:
    src = _tap_src()
    assert "tap.clear(); setHistory([]);" in src


def test_filter_still_applies_over_merged_buffer() -> None:
    src = _tap_src()
    # The class/session filter runs over the merged (history+live) buffer.
    assert "var out = allEvents;" in src


def test_bundle_transpiles_with_backfill() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "/* === components/workspace-tap.jsx === */" in body.decode("utf-8")
