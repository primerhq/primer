"""The per-class provider pages are gone and nothing still points at them.

Deleting a page is only half of it: a stale ROUTES key, a nav id, a
router pattern or a hint string that names the old path all leave the
console pointing somewhere that no longer exists. navigate() falls back
to "/" for an unknown key, so a missed reference lands the user on the
dashboard with no error anywhere.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def _route_keys() -> set[str]:
    table = re.search(r"const ROUTES = \{(.*?)\n    \};", _read("app.jsx"), re.S)
    assert table, "ROUTES table not found in app.jsx"
    return set(re.findall(r'^\s*"?([\w-]+)"?:', table.group(1), re.M))


def test_the_page_file_is_gone() -> None:
    assert not (UI / "components" / "providers.jsx").exists()


def test_nothing_in_the_console_still_names_the_component() -> None:
    for path in UI.rglob("*.jsx"):
        src = path.read_text(encoding="utf-8")
        assert "<ProvidersPage" not in src, path
        assert "window.ProvidersPage" not in src, path
    assert "components/providers.jsx" not in _read("index.html")


def test_the_three_routes_keys_are_gone() -> None:
    assert _route_keys().isdisjoint({"llm", "embedding", "rerank"})
    assert "providers" in _route_keys()


def test_the_path_parser_resolves_every_providers_path_to_the_catalog() -> None:
    src = _read("app.jsx")
    assert 'path.startsWith("/providers/llm")' not in src
    assert 'path.startsWith("/providers/embedding")' not in src
    assert 'path.startsWith("/providers/cross_encoder")' not in src
    assert 'if (root === "providers") return "providers";' in src


def test_the_router_table_dropped_the_per_class_patterns() -> None:
    src = _read("foundation/router.js")
    for pattern in (
        "/providers/llm",
        "/providers/llm/:id",
        "/providers/embedding",
        "/providers/embedding/:id",
        "/providers/cross_encoder",
        "/providers/cross_encoder/:id",
    ):
        assert f'"{pattern}"' not in src, pattern
    assert '{ pattern: "/providers",' in src


def test_the_sidebar_lost_the_three_entries() -> None:
    src = _read("components/chrome.jsx")
    for nav_id in ('id: "llm"', 'id: "embedding"', 'id: "rerank"'):
        assert nav_id not in src, nav_id


def test_the_docs_embeds_moved_to_the_catalog() -> None:
    src = _read("components/docs/embed-registry.jsx")
    assert '"ProvidersPage"' not in src
    # Four: the three model-family embeds plus web search, which folded
    # into the same catalog when its page was deleted.
    assert src.count('component: "ProviderCatalog"') == 4
    assert 'initialClass: "llm"' in src
    assert 'initialClass: "embedding"' in src
    assert 'initialClass: "cross_encoder"' in src
    assert 'initialClass: "web_search"' in src


def test_no_cross_page_hint_points_at_a_dead_path() -> None:
    """These strings are what an operator is told to type or click."""
    for rel in (
        "components/knowledge.jsx",
        "components/internal-collections.jsx",
        "components/approvals.jsx",
        "components/agents.jsx",
    ):
        src = _read(rel)
        for dead in ("/providers/llm", "/providers/embedding", "/providers/cross_encoder"):
            assert dead not in src, f"{rel} still points at {dead}"


def test_the_catalog_still_loads_after_the_profile_modal() -> None:
    """The catalog reuses MP_ProfileModal, so load order still matters
    even though providers.jsx no longer sits between them."""
    html = _read("index.html")
    assert html.index("components/model-profiles.jsx") < html.index(
        "components/provider-catalog.jsx"
    )


def test_the_studio2_legacy_table_points_at_the_catalog() -> None:
    """The trial console iframes classic-console paths. A row naming a
    path the router no longer resolves renders an empty frame with no
    error anywhere (ui/components/studio2/s2-legacy.jsx:36-44)."""
    src = _read("components/studio2/s2-legacy.jsx")
    for dead in (
        '"/providers/llm"',
        '"/providers/embedding"',
        '"/providers/cross_encoder"',
    ):
        assert dead not in src, dead
    assert '{ ref: "/providers",' in src


def test_the_web_search_page_is_gone() -> None:
    assert not (UI / "components" / "web_search.jsx").exists()
    assert "components/web_search.jsx" not in _read("index.html")


def test_nothing_still_renders_the_web_search_page() -> None:
    for path in UI.rglob("*.jsx"):
        src = path.read_text(encoding="utf-8")
        assert "WebSearchPage" not in src, path


def test_the_web_search_route_and_nav_entry_are_gone() -> None:
    assert "web-search" not in _route_keys()
    assert 'if (root === "web-search")' not in _read("app.jsx")
    assert 'id: "web-search"' not in _read("components/chrome.jsx")
    assert '"/web-search"' not in _read("foundation/router.js")


def test_the_web_search_docs_embed_moved_to_the_catalog() -> None:
    src = _read("components/docs/embed-registry.jsx")
    assert 'initialClass: "web_search"' in src


def test_the_reserved_row_guard_still_has_a_home() -> None:
    """DuckDuckGo cannot be deleted; the catalog shows the backend's 403
    rather than hiding the button behind a copied id list."""
    src = _read("components/provider-catalog.jsx")
    assert 'data-testid="provider-row-error"' in src


def test_the_providers_group_is_one_entry() -> None:
    """S4 section 6: one Providers entry, not a per-class list. The class
    rail inside the catalog is where the classes live now."""
    src = _read("components/chrome.jsx")
    group = src.split('group: "Providers"', 1)[1].split("group:", 1)[0]
    ids = re.findall(r'id:\s*"([\w-]+)"', group)
    assert ids == ["providers"], ids
    assert "providers" in _route_keys()


def test_the_class_pages_the_catalog_hosts_keep_their_routes() -> None:
    """Semantic search and channel providers lost their NAV entries, not
    their routes: their detail views still navigate back through them
    (ui/app.jsx:671,678,686,622)."""
    routes = _route_keys()
    assert routes >= {
        "semantic-search",
        "ssp-detail",
        "channel-providers",
        "channel-provider-detail",
    }
