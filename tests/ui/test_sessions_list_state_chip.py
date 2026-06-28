"""The sessions list derives its per-row chip from the shared decoder and
adds Needs-attention / Failed quick filters + an attention count."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
LIST = (UI / "components" / "sessions-list.jsx").read_text(encoding="utf-8")
CHROME = (UI / "components" / "chrome.jsx").read_text(encoding="utf-8")


def test_row_chip_uses_decoder() -> None:
    assert "describeSessionState" in LIST


def test_needs_attention_and_failed_filters() -> None:
    assert "needsAttention" in LIST
    low = LIST.lower()
    assert "needs attention" in low
    assert "failed" in low


def test_attention_count_surfaced() -> None:
    # The "N need attention" count is derived from the decoder and rendered
    # as an in-list triage surface (the nav-dot is a deferred fast-follow).
    assert "needsAttention" in LIST
    assert "need attention" in LIST.lower()


def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
