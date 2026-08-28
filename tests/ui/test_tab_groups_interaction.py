"""US-007 R2 phase 1 review finding #1, fixed in phase 2 (step 0).

nv-tab-groups.jsx has no jsdom in the py toolchain (confirmed alongside the
other nv-*.jsx statics, e.g. test_admin_users_page.py), so this is a static
source guard like its siblings (test_console_shell.py etc.), not a rendered
click simulation.

The group wrapper's onClick (focus-on-click) and a tab's onClick/
onDoubleClick (select/promote) all read the SAME model prop. Clicking or
double-clicking a tab in an UNFOCUSED group bubbles the tab's click up to
the group wrapper, which re-derives its own model from that same pre-click
closure and, called right after selectTab/promoteTab, can overwrite what
they just dispatched with a stale-model result - reverting the just-clicked
tab's activation. closeTab already stopped propagation for exactly this
reason; select and promote did not.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (
    ROOT / "ui" / "components" / "console" / "nv-tab-groups.jsx"
).read_text(encoding="utf-8")


def _fn_body(name: str) -> str:
    m = re.search(r"function " + name + r"\([^)]*\)\s*\{([\s\S]*?)\n  \}", SRC)
    assert m, f"{name} not found"
    return m.group(1)


def test_select_tab_stops_propagation_before_dispatching() -> None:
    body = _fn_body("selectTab")
    assert "ev.stopPropagation()" in body
    # Must guard THEN dispatch, matching closeTab's order - stopping
    # propagation after the model change would be too late.
    assert body.index("stopPropagation") < body.index("change(")


def test_promote_tab_stops_propagation_before_dispatching() -> None:
    body = _fn_body("promoteTab")
    assert "ev.stopPropagation()" in body
    assert body.index("stopPropagation") < body.index("change(")


def test_close_tab_still_stops_propagation() -> None:
    # Regression guard: the pattern this fix was modeled on must not regress.
    body = _fn_body("closeTab")
    assert "ev.stopPropagation()" in body


def test_the_dom_handlers_actually_pass_the_event_through() -> None:
    # A fix in the function body is dead unless the JSX wiring forwards the
    # real event - passing tab alone (dropping ev) would silently defeat it.
    assert re.search(
        r'onClick=\{function \(ev\) \{ selectTab\(tab, ev\); \}\}', SRC
    ), "tab onClick must forward ev to selectTab"
    assert re.search(
        r'onDoubleClick=\{function \(ev\) \{ promoteTab\(tab, ev\); \}\}', SRC
    ), "tab onDoubleClick must forward ev to promoteTab"


def test_bundle_transpiles_with_the_fix() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(ROOT / "ui")
    assert etag and body
