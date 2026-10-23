"""M11c: active defaults are managed IN the catalog, not a settings page.

Web search tests stay until Task 22. Speech tests were retired to Task 16.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_the_catalog_reads_and_writes_the_active_web_search_config() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "/web_search_active_config" in src
    assert "PC_ActiveWebSearchPanel" in src


def test_the_defaults_panels_carry_test_ids() -> None:
    src = _read("components/provider-catalog.jsx")
    assert 'data-testid="active-web-search-config"' in src


def test_the_web_search_panel_edits_both_config_modes() -> None:
    """ActiveWebSearchConfig is a discriminated union of single and
    aggregated; a panel that only writes single silently downgrades an
    aggregated install the first time an operator touches it."""
    src = _read("components/provider-catalog.jsx")
    assert '"aggregated"' in src
    assert "provider_ids" in src
    assert 'data-testid="active-web-search-mode"' in src


def test_the_aggregated_branch_sends_a_list_not_a_single_id() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "provider_ids: " in src
