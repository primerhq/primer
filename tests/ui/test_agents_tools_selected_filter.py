"""studio-ux fix 4 — the tool picker had a text filter but no way to see
WHICH tools are currently ticked across all toolsets/pages. A "Selected"
filter chip ANDs with the text filter, reusing the existing `selected`
Set the "selected · N" counter already reads.

RETARGET (uiv2 Wave 2): this behavior lived inline in AG_NewAgentModal
(ui/components/agents.jsx) - Wave 2 extracted it into the shared,
reusable ToolPicker (ui/components/shared/tool-picker.jsx, "build the
shared tool picker component here first" per the synthesis doc), so
every assertion here now reads that file instead. The behavior itself
is unchanged, just relocated + renamed to a controlled-component
contract (`selected`/`onChange` props instead of agent-specific state
names) so graphs/policies/services/the MCP allowlist can adopt it later.

Static-source checks only (the tests/ui suite convention — no DOM/browser
harness; see test_studio_activity.py's docstring for the rationale).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TOOL_PICKER = UI / "components" / "shared" / "tool-picker.jsx"


def _src() -> str:
    return TOOL_PICKER.read_text(encoding="utf-8")


def test_selected_only_state_exists() -> None:
    assert 'const [selectedOnly, setSelectedOnly] = React.useState(false);' in _src()


def test_filtered_toolset_entries_composes_selected_filter_with_text_filter() -> None:
    src = _src()
    assert "if (selectedOnly) {" in src
    assert "ts.tools.filter((t) => selected.has(t.scoped_id))" in src
    # Both filters can be active together — selected-only doesn't replace
    # the text-filter branch, it narrows its result further.
    assert "}, [toolsetEntries, filter, selectedOnly, selected]);" in src


def test_toggling_selected_only_resets_to_first_page() -> None:
    assert "React.useEffect(() => { setPage(1); }, [filter, selectedOnly]);" in _src()


def test_selected_filter_chip_rendered_next_to_the_search_input() -> None:
    src = _src()
    assert 'data-testid="tool-picker-filter-selected"' in src
    assert 'data-testid="tool-picker-filter"' in src
    search_idx = src.index('data-testid="tool-picker-filter"')
    chip_idx = src.index('data-testid="tool-picker-filter-selected"')
    # Chip sits right after the search box.
    assert search_idx < chip_idx
    assert "onClick={() => setSelectedOnly((v) => !v)}" in src
    assert "aria-pressed={selectedOnly}" in src


def test_selected_filter_chip_reuses_the_existing_selection_counter_state() -> None:
    # No new selection-tracking state — the chip reads/derives from the
    # SAME `selected` Set the "selected · N" counter uses.
    src = _src()
    assert "const selectedCount = selected.size;" in src


def test_empty_state_message_covers_the_selected_only_case() -> None:
    src = _src()
    assert 'data-testid="tool-picker-empty"' in src
    assert "No selected tools" in src


def test_bundle_transpiles_with_the_tools_selected_filter() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/shared/tool-picker.jsx === */" in text
