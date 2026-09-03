"""The shared y/w/r/n tool-capability badge primitive (R4 Intelligence group).

Mirrors the prototype's flagBadges()/FLAGS fixture (uiv2/Primer Console.dc.html):
four badges always render (yields/requires-workspace/role-gated/notifying),
dimmed when the flag does not apply, colored + full-opacity + strong border
when it does. Pinned the same way pager.jsx / entity-picker.jsx are in
test_pagination.py: primitive exists, is registered, is wired into a
representative consumer, and the bundle still transpiles.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
BADGES = UI / "components" / "shared" / "capability-badges.jsx"
INDEX = UI / "index.html"


def _src() -> str:
    return BADGES.read_text(encoding="utf-8")


def test_file_exists() -> None:
    assert BADGES.exists()


def test_component_and_helper_defined() -> None:
    src = _src()
    assert "function CapabilityBadges" in src
    assert "function capabilityFlags" in src


def test_exported_to_window() -> None:
    src = _src()
    assert "window.CapabilityBadges" in src
    assert "window.capabilityFlags" in src
    assert "ns.CapabilityBadges" in src
    assert "ns.capabilityFlags" in src


def test_four_flags_and_titles_match_the_prototype() -> None:
    src = _src()
    for k in ("y:", "w:", "r:", "n:"):
        assert k in src, k
    # Exact copy from uiv2/Primer Console.dc.html's FLAGS fixture.
    assert "yields" in src and "parks the run" in src
    assert "requires workspace" in src
    assert "role-gated" in src
    assert "notifying" in src and "fire and forget" in src


def test_derives_flags_from_the_four_catalogue_fields() -> None:
    # The four raw fields the batch-2 catalogue-badges backend work added.
    src = _src()
    for field in ("yields", "requires_workspace", "required_role", "tool_class"):
        assert field in src, field
    assert "notifying" in src


def test_required_testids_present() -> None:
    src = _src()
    assert 'data-testid="cap-badge-' in src or "testid" in src
    assert "cap-badge-" in src


def test_registered_in_index() -> None:
    text = INDEX.read_text(encoding="utf-8")
    assert "components/shared/capability-badges.jsx" in text


def test_loads_after_shared_before_agents() -> None:
    lines = [
        line for line in INDEX.read_text(encoding="utf-8").splitlines()
        if 'type="text/babel"' in line and "src=" in line
    ]
    order = []
    for line in lines:
        start = line.index('src="') + len('src="')
        end = line.index('"', start)
        order.append(line[start:end])
    badges_at = order.index("components/shared/capability-badges.jsx")
    assert badges_at > order.index("components/shared.jsx")
    assert order.index("components/agents.jsx") > badges_at


def test_agents_tool_picker_renders_the_badges() -> None:
    # RETARGET (uiv2 Wave 2): the agent form's tool picker was extracted
    # into the shared ToolPicker (ui/components/shared/tool-picker.jsx) -
    # the badges render there now, mounted by agents.jsx via
    # <window.ToolPicker>, not inline in agents.jsx itself.
    picker_src = (UI / "components" / "shared" / "tool-picker.jsx").read_text(encoding="utf-8")
    assert "CapabilityBadges" in picker_src
    agents_src = (UI / "components" / "agents.jsx").read_text(encoding="utf-8")
    assert "<window.ToolPicker " in agents_src


def test_bundle_transpiles_with_capability_badges() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/shared/capability-badges.jsx === */" in text
    assert "CapabilityBadges" in text
