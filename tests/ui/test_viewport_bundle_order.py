"""The bundler reads ui/index.html for ordered <script type=text/babel>
tags. viewport.js must load BEFORE any consumer (shared.jsx and every
page component) because the hook is read inside their bodies.
"""

from __future__ import annotations

from pathlib import Path

INDEX = Path(__file__).resolve().parents[2] / "ui" / "index.html"


def _script_order() -> list[str]:
    html = INDEX.read_text(encoding="utf-8")
    out = []
    for line in html.splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_viewport_listed_in_index() -> None:
    assert "foundation/viewport.js" in _script_order()


def test_viewport_loads_before_shared() -> None:
    order = _script_order()
    assert order.index("foundation/viewport.js") < order.index(
        "components/shared.jsx"
    )


def test_viewport_loads_before_chrome() -> None:
    order = _script_order()
    assert order.index("foundation/viewport.js") < order.index(
        "components/console/nv-chrome.jsx"
    )


def test_viewport_loads_before_the_console_shell() -> None:
    """US-014 M1: NV_Shell reads useViewport().isMobile at render time to
    pick its chrome - same load-before-consumer contract as every other
    reader here."""
    order = _script_order()
    assert order.index("foundation/viewport.js") < order.index(
        "components/console/nv-shell.jsx"
    )


def test_mobile_shell_loads_before_the_console_shell() -> None:
    order = _script_order()
    assert order.index("components/console/nv-mobile-shell.jsx") < order.index(
        "components/console/nv-shell.jsx"
    )
