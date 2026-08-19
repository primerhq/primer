"""knowledge.jsx v2: tree browser, grep box, search settings, import."""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "knowledge.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_tree_endpoints_used() -> None:
    src = _src()
    assert "/docs" in src and "parent=" in src and "depth=" in src


def test_grep_box_present() -> None:
    src = _src()
    assert "/grep" in src and "path_prefix" in src


def test_search_settings_lifecycle() -> None:
    src = _src()
    assert "PUT" in src and "/search" in src
    assert "indexing" in src and "error" in src


def test_import_posts_multipart() -> None:
    assert "/import" in _src()


def test_system_lock_no_edit() -> None:
    src = _src()
    assert "system" in src and ("lock" in src or "Lock" in src)


def test_page_export_name_is_pinned() -> None:
    # [CROSSPLAN 2026-08-16, F33] S8's overlay host binds window.CollectionsPage.
    src = _src()
    assert "window.CollectionsPage = CollectionsPage;" in src
    assert "window.DocumentsPage" not in src


def test_flat_documents_page_is_gone() -> None:
    assert "_convert_file" not in _src()
