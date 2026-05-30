"""session-detail.jsx wraps its split-pane layout in MobileTabs when
useViewport().isMobile, with tabs Overview / Messages / State / Files.
Active tab is read from the URL hash query 'tab'."""
from __future__ import annotations
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2] / "ui" / "components" / "session-detail.jsx"
)


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_use_viewport() -> None:
    assert "useViewport" in _src()


def test_mobile_tabs_used() -> None:
    assert "MobileTabs" in _src()


def test_tab_ids_present() -> None:
    src = _src()
    for tab in ("overview", "messages", "state", "files"):
        assert tab in src, f"missing tab '{tab}'"


def test_tab_state_uses_router_query() -> None:
    src = _src()
    assert "query.tab" in src or 'query["tab"]' in src
