"""The controls the broadened a11y sweep found are named (console review C-003, review of #668).

The standing sweep (``tests/ui_e2e/test_console_controls_have_names_sweep.py``) takes each control's name from Chromium and opens every Platform page as an overlay AND as a view, every System
view and every create form. Run on the Platform views and the forms it newly opens, it found 13 controls whose only text was a placeholder or a visible label that points at nothing:

* the Configure form of the internal collections: the Semantic Search provider, the embedding provider and the embedding model selects (and the reranker model select beside them);
* the New profile form: the id, the description and the context length inputs;
* the New policy form: the id and the timeout inputs;
* the MCP page: the endpoint URL box and the tool search box;
* the profile page: the current and the new password inputs;
* the entity picker's results list (``role="listbox"``), which the graph builder's agent pickers draw.

This is the static half: each control's tag carries the name (``aria-label``, or an ``id`` that its visible ``<label htmlFor>`` points at). The browser half is the sweep itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui" / "components"


def _src(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


def _tag(src: str, anchor: str) -> str:
    """The whole opening tag that contains ``anchor`` (a unique piece of it)."""
    at = src.index(anchor)
    start = max(src.rfind(open_, 0, at + len(open_)) for open_ in ("<input", "<select", "<div", "<textarea"))   # the nearest tag start, the anchor's own included
    depth, quote, i = 0, "", start
    while i < len(src):
        c = src[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth <= 0:
            return src[start:i + 1]
        i += 1
    raise AssertionError(anchor)


# (file, a piece of the control's tag, how it is named): ("label", id) = it has that id and a <label htmlFor=id> exists; ("aria", text) = aria-label=text
CONTROLS = [
    ("internal-collections.jsx", "onChange={(e) => setSearchProviderId(e.target.value)}", ("label", "ic-search-provider")),
    ("internal-collections.jsx", "onChange={(e) => setProviderId(e.target.value)}", ("label", "ic-embedding-provider")),
    ("internal-collections.jsx", "onChange={(e) => setModel(e.target.value)}", ("label", "ic-embedding-model")),
    ("internal-collections.jsx", "onChange={(e) => setRerankerModel(e.target.value)}", ("label", "ic-reranker-model")),
    ("model-profiles.jsx", "onChange={(e) => setId(e.target.value)}\n          placeholder=\"gx10--qwen-fast\"", ("label", "mp-id")),
    ("model-profiles.jsx", "onChange={(e) => setDescription(e.target.value)}", ("label", "mp-description")),
    ("model-profiles.jsx", "onChange={(e) => setContextLength(e.target.value)}", ("label", "mp-context-length")),
    ("approvals.jsx", 'data-testid="approval-policy-id"', ("label", "approval-policy-id-input")),
    ("approvals.jsx", 'data-testid="approval-policy-timeout"', ("label", "approval-policy-timeout-input")),
    ("mcp.jsx", 'data-testid="mcp-endpoint-url"', ("label", "mcp-endpoint-url-input")),
    ("mcp.jsx", 'data-testid="mcp-tool-search"', ("aria", "Search exposed tools")),
    ("console/nv-system.jsx", 'data-testid="nv-pw-current"', ("aria", "Current password")),
    ("console/nv-system.jsx", 'data-testid="nv-pw-next"', ("aria", "New password")),
    ("shared/entity-picker.jsx", 'role="listbox"', ("aria", "{")),   # named after the picker's own label or placeholder (a JSX expression)
]


@pytest.mark.parametrize(("file", "anchor", "how"), CONTROLS, ids=[f"{f}:{h[1]}" for f, _a, h in CONTROLS])
def test_the_control_found_by_the_sweep_is_named(file: str, anchor: str, how: tuple[str, str]) -> None:
    src = _src(file)
    tag = _tag(src, anchor)
    kind, value = how
    if kind == "aria":
        assert re.search(r"\baria-label=", tag), tag
        if value != "{":
            assert f'aria-label="{value}"' in tag, tag
    else:
        assert f'id="{value}"' in tag, tag
        assert re.search(rf'<label[^>]*\bhtmlFor="{re.escape(value)}"', src), f"no <label htmlFor={value!r}> points at it"


def test_the_pickers_results_list_is_named_like_its_search_box() -> None:
    src = _src("shared/entity-picker.jsx")
    tag = _tag(src, 'role="listbox"')
    assert "aria-label={label || props.ariaLabel || placeholder}" in tag, tag
