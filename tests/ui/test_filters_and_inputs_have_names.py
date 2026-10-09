"""The Platform pages' list filters and plain create-form inputs name themselves (console review C-003, found by the runtime sweep of the main surfaces).

Each of these controls was an input or a select whose only text was a placeholder or its first option. They carry an ``aria-label`` now. One row per site: the file, an anchor that sits in or
right after the control's opening tag (``occurrence`` picks the n-th match when the anchor repeats), and the attribute the tag must carry. The #668 review (2026-10-09) found two more of the
same class: the platform approvals' decision-audit sort select and the channel rules page's rule filter. The runtime check is
``tests/ui_e2e/test_platform_filters_and_inputs_named_journey.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.ui.test_graph_builder_named_controls import _tag_end

UI = Path(__file__).resolve().parents[2] / "ui" / "components"

# (file, anchor, occurrence, attribute the control's tag must carry)
SITES = [
    ("agents.jsx", 'placeholder="Filter agents\u2026"', 0, 'aria-label="Filter agents"'),
    ("graphs.jsx", 'placeholder="Filter graphs\u2026"', 0, 'aria-label="Filter graphs"'),
    ("toolsets.jsx", 'placeholder="Filter toolsets\u2026"', 0, 'aria-label="Filter toolsets"'),
    ("toolsets.jsx", '<option value="">all kinds</option>', 0, 'aria-label="Filter by kind"'),
    ("services.jsx", 'data-testid="services-filter"', 0, 'aria-label="Filter services"'),
    ("workspaces.jsx", 'placeholder="Filter workspaces\u2026"', 0, 'aria-label="Filter workspaces"'),
    ("workspaces.jsx", 'data-testid="workspaces-template-filter"', 0, 'aria-label="Filter by template"'),
    ("workspaces.jsx", 'data-testid="workspaces-provider-filter"', 0, 'aria-label="Filter by provider"'),
    ("workspaces.jsx", 'placeholder="e.g. Investing research"', 0, 'aria-label="Workspace name"'),
    ("workspaces.jsx", 'placeholder="e.g. Investing research"', 1, 'aria-label="Workspace name"'),
    ("channels.jsx", 'placeholder="Filter channels\u2026"', 0, 'aria-label="Filter channels"'),
    ("channels.jsx", '<option value="">all providers</option>', 0, 'aria-label="Filter by provider"'),
    ("channels.jsx", 'placeholder="Filter providers\u2026"', 0, 'aria-label="Filter providers"'),
    ("channels.jsx", '<option value="">all platforms</option>', 0, 'aria-label="Filter by platform"'),
    ("channel_rules.jsx", '<option value="">all providers</option>', 0, 'aria-label="Filter by provider"'),
    ("channel_rules.jsx", 'placeholder="Filter rules\u2026"', 0, 'aria-label="Filter rules"'),
    ("provider-catalog.jsx", 'data-testid="provider-filter"', 0, 'aria-label="Filter providers"'),
    ("provider-catalog.jsx", 'placeholder="stt_provider_id"', 0, 'aria-label="Speech-to-text provider id"'),
    ("provider-catalog.jsx", 'placeholder="tts_provider_id"', 0, 'aria-label="Text-to-speech provider id"'),
    ("provider-catalog.jsx", 'data-testid="active-speech-voice"', 0, 'aria-label="Voice"'),
    ("shell/sh-activity.jsx", 'data-testid={"activity-filter:" + key}', 0, "aria-label={"),
    ("knowledge.jsx", 'placeholder="id (optional)"', 0, 'aria-label="Collection id"'),
    ("knowledge.jsx", 'placeholder="Description"', 0, 'aria-label="Collection description"'),
    ("console/nv-overlays.jsx", 'placeholder="extra init command"', 0, 'aria-label="Extra init command"'),
    ("console/nv-platform.jsx", 'data-testid="nv-audit-sort-by"', 0, 'aria-label="Sort decisions"'),
    ("shared/tool-picker.jsx", 'data-testid="tool-picker-filter"', 0, 'aria-label="Search the tool catalog"'),
    ("shared/tool-picker.jsx", "data-testid={`tool-picker-group-${entry.id}`}", 0, "aria-label={"),
    ("semantic-search.jsx", 'placeholder="Filter providers\u2026"', 0, 'aria-label="Filter providers"'),
]


def _tag_around(src: str, anchor: str, occurrence: int) -> str:
    """The opening tag (``<input``, ``<select``) the n-th ``anchor`` sits in, or follows."""
    at = -1
    for _ in range(occurrence + 1):
        at = src.index(anchor, at + 1)
    start = max(src.rfind("<input", 0, at), src.rfind("<select", 0, at))
    assert start >= 0, f"no <input or <select before {anchor!r}"
    return src[start:_tag_end(src, start + 1)]


@pytest.mark.parametrize(("file", "anchor", "occurrence", "attribute"), SITES, ids=[f"{s[0]}:{s[1][:40]}#{s[2]}" for s in SITES])
def test_the_control_carries_its_name(file: str, anchor: str, occurrence: int, attribute: str) -> None:
    src = (UI / file).read_text(encoding="utf-8")
    tag = _tag_around(src, anchor, occurrence)
    assert attribute in tag, f"{file}: {anchor!r} sits in a tag without {attribute}:\n{tag[:200]}"


def test_the_tag_finder_reads_the_tag_an_anchor_sits_in() -> None:
    src = '<div><input\n  className="a"\n  placeholder="x"\n  onChange={(e) => go(e > 1)}\n/>\n<select value={v}>\n  <option value="">all</option>\n</select></div>'
    assert _tag_around(src, 'placeholder="x"', 0).startswith("<input") and "go(e > 1)" in _tag_around(src, 'placeholder="x"', 0)
    assert _tag_around(src, '<option value="">all</option>', 0).startswith("<select value={v}>")
    assert re.fullmatch(r"<select value=\{v\}>", _tag_around(src, '<option value="">all</option>', 0))
