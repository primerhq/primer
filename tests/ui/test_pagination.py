"""Structural checks for the shared list pagination primitive (bug #19).

The backend list endpoints already paginate (OffsetPageResponse,
``?limit=&offset=``); the console previously fetched a hard ``limit=200``
and dumped everything. This suite pins the shared ``usePagedList`` hook +
``Pager`` component: that they exist, are registered in the bundle in the
right order, expose the required testids, and are actually wired into a
few representative list pages.

Static-source + bundle-build checks only (matching the rest of the ui/
suite — no React render).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PAGER = UI / "components" / "shared" / "pager.jsx"
INDEX = UI / "index.html"


def _pager_src() -> str:
    return PAGER.read_text(encoding="utf-8")


# ---- The primitive exists + is defined ---------------------------------


def test_pager_file_exists() -> None:
    assert PAGER.exists()


def test_hook_and_component_defined() -> None:
    src = _pager_src()
    assert "function usePagedList" in src
    assert "function Pager" in src


def test_exported_to_window() -> None:
    src = _pager_src()
    # Both the bare globals and the primerApi namespace, so call sites can
    # destructure from window.primerApi like the rest of the app.
    assert "window.usePagedList" in src
    assert "window.Pager" in src
    assert "ns.usePagedList" in src
    assert "ns.Pager" in src


# ---- Required testids --------------------------------------------------


def test_required_testids_present() -> None:
    src = _pager_src()
    for tid in ("pager", "pager-range", "pager-prev", "pager-next"):
        assert f'data-testid="{tid}"' in src, f"missing testid {tid}"


# ---- Uses the real OffsetPageResponse fields ---------------------------


def test_relies_on_offsetpage_fields() -> None:
    src = _pager_src()
    # limit/offset query params + items/total response fields.
    assert "limit=" in src and "offset=" in src
    assert ".items" in src
    assert ".total" in src


def test_infers_has_next_without_total() -> None:
    # total may be null -> a full page implies there may be more.
    src = _pager_src()
    assert "items.length >= pageSize" in src


def test_default_page_size_is_50() -> None:
    assert "DEFAULT_PAGE_SIZE = 50" in _pager_src()


def test_uses_distinct_css_class_not_pager() -> None:
    # Named .list-pager so it never collides with the older hand-rolled
    # `.tbl-foot .pager` markup still present on a few pages.
    assert "list-pager" in _pager_src()


# ---- Registered in the bundle, in the right order ----------------------


def _bundle_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


def test_registered_in_index() -> None:
    assert "components/shared/pager.jsx" in _bundle_order()


def test_loads_after_shared_before_pages() -> None:
    order = _bundle_order()
    pager_at = order.index("components/shared/pager.jsx")
    # After shared.jsx (needs Btn / Icon).
    assert pager_at > order.index("components/shared.jsx")
    # Before the page components that consume it.
    for page in (
        "components/agents.jsx",
        "components/providers.jsx",
        "components/chats.jsx",
    ):
        assert order.index(page) > pager_at, f"{page} loads before pager.jsx"


# ---- Representative pages actually use the primitive -------------------

REPRESENTATIVE = [
    "agents.jsx",
    "graphs.jsx",
    "chats.jsx",
    "providers.jsx",
    "channels.jsx",
    "triggers.jsx",
    "harnesses.jsx",
    "semantic-search.jsx",
    "knowledge.jsx",
    "workspaces.jsx",
]


def test_representative_pages_use_hook_and_component() -> None:
    for name in REPRESENTATIVE:
        src = (UI / "components" / name).read_text(encoding="utf-8")
        assert "usePagedList" in src, f"{name} does not use usePagedList"
        assert "Pager" in src, f"{name} does not render Pager"


def test_workspace_subpages_use_hook() -> None:
    for name in ("templates.jsx", "providers.jsx"):
        src = (UI / "components" / "workspaces" / name).read_text(encoding="utf-8")
        assert "usePagedList" in src, f"workspaces/{name} missing usePagedList"


# ---- The whole bundle still transpiles with the new primitive ----------


def test_bundle_transpiles_with_pager() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/shared/pager.jsx === */" in text
    assert "usePagedList" in text
