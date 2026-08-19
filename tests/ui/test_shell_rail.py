"""Rail discipline (spec section 8): exactly three place-y lists.

Prohibited: a rail used as a utility junk drawer, and a sidebar that
forgets personalization. Both are checkable in source: the list set is a
frozen literal, and the prefs are keyed by the authenticated account.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-rail.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_exactly_three_lists() -> None:
    src = _src()
    m = re.search(r"var SH_RAIL_LISTS = \[([^\]]*)\]", src)
    assert m, "SH_RAIL_LISTS must be a literal so the rail cannot grow quietly"
    names = re.findall(r'"([a-z]+)"', m.group(1))
    assert names == ["sessions", "files", "attention"]


def test_global_utilities_are_not_in_the_rail() -> None:
    src = _src()
    for junk in ("Settings", "Docs", "Providers", "Admin"):
        assert junk not in src, f"{junk} belongs in the top bar, not the rail"


def test_personalization_is_keyed_by_account() -> None:
    src = _src()
    assert "SH_railPrefsKey" in src
    assert '"primer.shell.rail:"' in src
    assert "localStorage" in src
    for field in ("order", "hidden", "badgeStyle", "collapsed"):
        assert field in src, field


def test_sessions_are_frecency_ordered_with_status_and_nesting() -> None:
    src = _src()
    assert "parent_session_id" in src, "parent nesting is contract"
    assert "SH_statusLine" in src, "rail row chips render the same status string"
    assert "frecency" in src


def test_attention_counts_come_from_pending_yields() -> None:
    src = _src()
    assert "SH_api.pendingYields" in src
    assert 'data-testid="rail-attention"' in src


def test_rows_render_verbs_from_the_registry() -> None:
    """Dual-render rule: rail rows are registry-rendered affordances."""
    src = _src()
    assert 'forSurface("rail")' in src
