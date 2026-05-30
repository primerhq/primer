"""shared/card-list.jsx, shared/bottom-sheet.jsx, shared/mobile-tabs.jsx,
shared/floating-action.jsx must load after components/shared.jsx and
before any page component."""
from __future__ import annotations
from pathlib import Path

INDEX = Path(__file__).resolve().parents[2] / "ui" / "index.html"

def _order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out

PRIMITIVES = [
    "components/shared/card-list.jsx",
    "components/shared/bottom-sheet.jsx",
    "components/shared/mobile-tabs.jsx",
    "components/shared/floating-action.jsx",
]

def test_all_primitives_registered() -> None:
    order = _order()
    for p in PRIMITIVES:
        assert p in order, f"{p} missing from bundle"

def test_primitives_load_after_shared_jsx() -> None:
    order = _order()
    shared_at = order.index("components/shared.jsx")
    for p in PRIMITIVES:
        assert order.index(p) > shared_at, f"{p} loads before shared.jsx"

def test_primitives_load_before_page_components() -> None:
    order = _order()
    last_primitive = max(order.index(p) for p in PRIMITIVES)
    for page in (
        "components/dashboard.jsx",
        "components/sessions-list.jsx",
        "components/workspaces.jsx",
        "components/chats.jsx",
    ):
        assert order.index(page) > last_primitive, (
            f"{page} loads before all primitives"
        )
