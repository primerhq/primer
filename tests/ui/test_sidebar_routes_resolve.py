"""Every sidebar entry must resolve to a real path.

``navigate()`` maps a nav id to a URL through its ``ROUTES`` table and ends
with ``route || "/"``. That fallback means an id missing from the table does
not throw: it silently rewrites the hash to ``#/`` and drops the user on the
dashboard. Clicking the item simply appears to do nothing, and nothing shows
up in the console or the network log, so the only way to notice is to click
every entry by hand.

Two entries shipped that way -- ``studio2`` and ``services`` -- both added to
the sidebar without the matching ROUTES key. This pins the pair so the next
nav addition fails here instead of in someone's browser.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"

# navigate() intercepts these before the table lookup, so they need no entry.
SPECIAL_CASED = {"studio"}


def _nav_items() -> list[dict[str, str]]:
    """Parse the sidebar NAV literal into {id, label, href?} dicts."""
    src = (UI / "components" / "chrome.jsx").read_text(encoding="utf-8")
    items = []
    for block in re.findall(r"\{\s*id:\s*\"[\w-]+\".*?\}", src, re.S):
        ident = re.search(r"id:\s*\"([\w-]+)\"", block)
        label = re.search(r"label:\s*\"([^\"]+)\"", block)
        if not ident or not label:
            continue
        entry = {"id": ident.group(1), "label": label.group(1)}
        if "href:" in block:
            entry["href"] = "yes"
        items.append(entry)
    return items


def _route_keys() -> set[str]:
    src = (UI / "app.jsx").read_text(encoding="utf-8")
    table = re.search(r"const ROUTES = \{(.*?)\n    \};", src, re.S)
    assert table, "ROUTES table not found in app.jsx; update this test"
    return set(re.findall(r"^\s*\"?([\w-]+)\"?:", table.group(1), re.M))


def test_every_sidebar_item_has_a_route() -> None:
    routes = _route_keys()
    items = _nav_items()
    assert len(items) > 20, f"nav parse looks wrong, got {len(items)} items"

    # href entries render as plain anchors and never reach navigate().
    unrouted = [
        i for i in items if "href" not in i and i["id"] not in routes | SPECIAL_CASED
    ]
    assert not unrouted, (
        "sidebar entries with no ROUTES entry (clicking them silently lands "
        f"on the dashboard): {[(i['id'], i['label']) for i in unrouted]}"
    )


def test_studio2_and_services_routes_match_their_pages() -> None:
    """The two that regressed, pinned against the paths their pages parse."""
    routes = _route_keys()
    src = (UI / "app.jsx").read_text(encoding="utf-8")

    assert routes >= {"studio2", "services"}
    # S2_RootGate gates on the path prefix; the services page resolves off the
    # first path segment. Both must agree with what ROUTES emits.
    assert 'path.startsWith("/studio2")' in src
    assert 'if (root === "services") return "services";' in src
    assert re.search(r'studio2:\s*"/studio2"', src)
    assert re.search(r'services:\s*"/services"', src)
