"""FD1a — the old full-page SessionDetail component is gone.

The Studio (/workspaces/:wid) subsumes the session view, so /sessions/:id now
resolves the session's workspace and redirects into the Studio. The dead page
component and its window.SessionDetail export were removed from
session-detail.jsx. This guards that:
  - the page component + its global stay deleted (and no other bundled file
    re-introduces them);
  - the exports the Studio reuses (SessionLiveStream, SD_GraphRunView) survive;
  - app.jsx keeps the /sessions/:id -> Studio redirect + the in-flight
    placeholder, and never mounts the removed page.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
DETAIL = (UI / "components" / "session-detail.jsx").read_text(encoding="utf-8")
APP = (UI / "app.jsx").read_text(encoding="utf-8")


def _bundled_jsx() -> list[Path]:
    return sorted((UI / "components").rglob("*.jsx")) + sorted(UI.glob("*.jsx"))


def test_session_detail_page_component_removed() -> None:
    assert "function SessionDetail(" not in DETAIL, "the dead SessionDetail page component must stay removed"
    assert "window.SessionDetail" not in DETAIL, "the window.SessionDetail export must stay removed"


def test_no_bundled_file_reintroduces_the_page() -> None:
    for f in _bundled_jsx():
        src = f.read_text(encoding="utf-8")
        assert "function SessionDetail(" not in src, f"{f.name} re-defines the removed SessionDetail page"
        assert "window.SessionDetail " not in src and "window.SessionDetail=" not in src, (
            f"{f.name} re-exports window.SessionDetail"
        )


def test_studio_reused_exports_survive() -> None:
    assert "window.SessionLiveStream = SessionLiveStream" in DETAIL
    assert "window.SD_GraphRunView = SD_GraphRunView" in DETAIL


def test_sessions_id_redirect_intact() -> None:
    # /sessions/:id resolves the workspace and deep-links into the Studio.
    assert "?open=session:" in APP, "deep-link into the Studio must remain"
    assert "#/workspaces/" in APP, "must redirect into the workspace Studio"
    assert "Opening session" in APP, "the in-flight redirect placeholder must remain"
    # The removed page component is never mounted.
    assert "<SessionDetail" not in APP
    assert "createElement(SessionDetail" not in APP


def test_bundle_transpiles_without_the_page() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
