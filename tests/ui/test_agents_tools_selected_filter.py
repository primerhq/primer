"""studio-ux fix 4 — the Edit/New-agent modal's Tools tab
(`AG_NewAgentModal` in ui/components/agents.jsx) had a text filter but no
way to see WHICH tools are currently ticked across all toolsets/pages. A
"Selected" filter chip now ANDs with the text filter, reusing the existing
`selectedScopedIds` state the "N of 172 selected" counter already reads.

Static-source checks only (the tests/ui suite convention — no DOM/browser
harness; see test_studio_activity.py's docstring for the rationale).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
AGENTS = UI / "components" / "agents.jsx"


def _src() -> str:
    return AGENTS.read_text(encoding="utf-8")


def _fn_block(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _modal_src() -> str:
    return _fn_block(_src(), "function AG_NewAgentModal(", "function AgentDetail(")


def test_selected_only_state_exists() -> None:
    modal = _modal_src()
    assert "const [showSelectedOnly, setShowSelectedOnly] = React.useState(false);" in modal


def test_filtered_toolset_entries_composes_selected_filter_with_text_filter() -> None:
    modal = _modal_src()
    assert "if (showSelectedOnly) {" in modal
    assert "ts.tools.filter((t) => selectedScopedIds.has(t.scoped_id))" in modal
    # Both filters can be active together — selected-only doesn't replace
    # the text-filter branch, it narrows its result further.
    assert "}, [toolsetEntries, toolFilter, showSelectedOnly, selectedScopedIds]);" in modal


def test_toggling_selected_only_resets_to_first_page() -> None:
    modal = _modal_src()
    assert "React.useEffect(() => { setToolPage(1); }, [toolFilter, showSelectedOnly]);" in modal


def test_selected_filter_chip_rendered_next_to_the_search_input() -> None:
    modal = _modal_src()
    assert 'data-testid="agent-tool-filter-selected"' in modal
    assert 'data-testid="agent-tool-filter"' in modal
    search_idx = modal.index('data-testid="agent-tool-filter"')
    chip_idx = modal.index('data-testid="agent-tool-filter-selected"')
    counter_idx = modal.index("{selectedCount} of {totalAvailable} selected")
    # Chip sits between the search box and the "N of M selected" counter.
    assert search_idx < chip_idx < counter_idx
    assert "onClick={() => setShowSelectedOnly((v) => !v)}" in modal
    assert 'aria-pressed={showSelectedOnly}' in modal


def test_selected_filter_chip_reuses_the_existing_selection_counter_state() -> None:
    # No new selection-tracking state — the chip reads/derives from the
    # SAME selectedScopedIds Set the "N of 172 selected" counter uses.
    modal = _modal_src()
    assert "const selectedCount = selectedScopedIds.size;" in modal


def test_empty_state_message_covers_the_selected_only_case() -> None:
    modal = _modal_src()
    assert 'data-testid="agent-tool-empty"' in modal
    assert "No selected tools" in modal


def test_bundle_transpiles_with_the_tools_selected_filter() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/agents.jsx === */" in text
