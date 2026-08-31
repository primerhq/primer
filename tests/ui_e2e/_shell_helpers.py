"""Playwright helpers for the three-view console (flag day P7).

The facade amendment M16 asks for: every ui_e2e test drives the console
through a helper layer, so re-pointing at a new shell is one edit here
rather than N edits across the suite. That bet paid out three times now -
first S8's fresh shell, then the three-view console, now uiv2 R2's rail +
tab-group split view.

Selectors mirror ui/components/console/*.jsx exactly:

  nv-root / nv-topbar / nv-actbar / nv-center
  nv-tg-tab:<kind>:<ref> (nv-tab-groups.jsx) · nv-overlay:<name> · nv-palette
  nv-rail-inbox / nv-rail-inbox-row:<sid> (cross-workspace, attention-only)
  nv-rail-ws:<wid> / nv-rail-ws-session:<sid> (workspace tree, collapsed
  by default - expand the workspace row before a tree row is reachable)
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

SHELL_CONSOLE_IGNORES = [
    r"net::ERR_ABORTED",
    r"favicon",
    r"status of 404",
]


def shell_url(console_url: str, wid: str) -> str:
    return f"{console_url}#/w/{wid}"


def open_shell(page: Page, console_url: str, wid: str, *, timeout: int = 20_000) -> None:
    page.goto(shell_url(console_url, wid))
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=timeout)


def open_doc(page: Page, console_url: str, wid: str, kind: str, ref: str,
             *, anchor: str | None = None, timeout: int = 45_000) -> None:
    """Deep-link a document open and wait for its tab.

    The budget is deliberately generous. Reaching a document in another
    workspace is a chain, not a navigation: the console boots on
    whichever workspace it lands in first, then follows the url to the
    target workspace and opens this doc. 20 s covered that on an idle
    runner and not on a busy one.
    """
    url = f"{console_url}#/w/{wid}?doc={kind}:{ref}"
    if anchor:
        url += f"#{anchor}"
    page.goto(url)
    # RETARGET (uiv2 R2): the tab-group host's tabs carry the nv-tg-
    # prefix (nv-tab-groups.jsx); the doc= grammar and restore are
    # unchanged.
    expect(page.get_by_test_id(f"nv-tg-tab:{kind}:{ref}")).to_be_visible(
        timeout=timeout)


def open_overlay(page: Page, console_url: str, wid: str, name: str,
                 *, timeout: int = 20_000) -> None:
    page.goto(f"{console_url}#/w/{wid}?overlay={name}")
    expect(page.get_by_test_id(f"nv-overlay:{name}")).to_be_visible(timeout=timeout)


def open_palette(page: Page) -> None:
    """Open the command palette, by its own affordance.

    Clicks the topbar search field rather than pressing Ctrl+K: the
    field is the persistent, visible way in, so a caller checking that
    a surface is reachable without typing a URL is checking the path a
    person would actually take; the chord has its own coverage in
    tests/ui.
    """
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    chip = page.get_by_test_id("nv-search-btn")
    expect(chip).to_be_visible(timeout=20_000)
    chip.click()
    expect(page.get_by_test_id("nv-palette")).to_be_visible(timeout=10_000)


def run_verb(page: Page, label: str) -> None:
    open_palette(page)
    page.get_by_test_id("nv-palette-input").fill(label)
    page.get_by_test_id("nv-palette-row").first.click()


def session_row(page: Page, sid: str, wid: str, *, timeout: int = 10_000):
    """The workspace tree row for a specific session id.

    RETARGET (uiv2 R2): the flat per-workspace band list is now the
    Inbox (attention-only, cross-workspace - see attention_item below)
    plus the workspace tree, which starts collapsed. wid is required so
    the right nv-rail-ws:<wid> row can be expanded; the expand is
    idempotent (checks the target row first) so a second call in the
    same test does not re-toggle and collapse it.
    """
    ws_row = page.get_by_test_id(f"nv-rail-ws:{wid}")
    expect(ws_row).to_be_visible(timeout=timeout)
    row = page.get_by_test_id(f"nv-rail-ws-session:{sid}")
    if row.count() == 0:
        ws_row.click()
    return row


def attention_item(page: Page, sid: str):
    """The attention affordance for a session.

    RETARGET (uiv2 R2): needs-a-human sessions now lead the Inbox, the
    cross-workspace attention feed (notes 2.1), not a per-workspace band
    - clicking it opens the session whose inline card carries the
    decision, same as before.
    """
    return page.get_by_test_id(f"nv-rail-inbox-row:{sid}")


# ---------------------------------------------------------------------------
# Legacy route translation
# ---------------------------------------------------------------------------
#
# Every management page the console used to serve at its own "#/<page>"
# route is an overlay on the console. The e2e suite predates that move
# and navigates by page name, so translate here rather than in N tests:
# one table, one edit whenever a surface moves again.
#
# Keys are the legacy route path with no leading "#/"; values are the
# overlay target ("<name>[:<section>[:<id>]]"). A route that takes an id
# uses "{id}" in the target, filled from the caller's trailing segment.

LEGACY_ROUTE_OVERLAYS = {
    "agents": "agents",
    "graphs": "graphs",
    "triggers": "triggers",
    "toolsets": "toolsets",
    "tools": "tools",
    "approvals": "approvals",
    "workers": "workers",
    "health": "workers:health",
    "harnesses": "harnesses",
    "services": "services",
    "workspaces": "workspaces",
    "workspaces/templates": "workspaces:templates",
    "workspaces/providers": "providers:workspace",
    "channels": "channels",
    "channels/channels": "channels",
    "channels/associations": "channels:rules",
    "channels/rules": "channels:rules",
    "channels/providers": "providers:channel",
    "knowledge/collections": "collections",
    "subsystems/internal-collections": "internal-collections",
    "ssp": "providers:ssp",
    "providers": "providers",
    "providers/llm": "providers:llm",
    "providers/embedding": "providers:embedding",
    "providers/cross_encoder": "providers:cross_encoder",
    "providers/stt": "providers:stt",
    "providers/tts": "providers:tts",
    "providers/web_search": "providers:web_search",
    "providers/web_fetch": "providers:web_fetch",
    "providers/artifact_storage": "providers:artifact_storage",
}


def overlay_target(route: str) -> str:
    """Translate a legacy console route into an overlay target.

    ``route`` may carry a trailing record id ("agents/ag-1"), which lands
    on the overlay's own id slot. Raises rather than guessing: a route
    with no successor is a surface that lost its home, and silently
    navigating nowhere is how the old provider-catalog helper hid the
    fact that it did nothing at all.
    """
    path = route.lstrip("#").lstrip("/").rstrip("/")
    if path in LEGACY_ROUTE_OVERLAYS:
        return LEGACY_ROUTE_OVERLAYS[path]
    head, _, tail = path.rpartition("/")
    if head in LEGACY_ROUTE_OVERLAYS and tail:
        target = LEGACY_ROUTE_OVERLAYS[head]
        # The overlay grammar is name[:section[:id]], so a target that
        # already names a section takes the id in third position and one
        # that does not has to leave the section slot empty.
        return f"{target}:{tail}" if ":" in target else f"{target}::{tail}"
    raise AssertionError(
        f"no console overlay for legacy route {route!r}; add it to "
        "LEGACY_ROUTE_OVERLAYS or drive the surface it moved to"
    )


def wait_for_overlay_url(page: Page, route: str, *,
                         timeout: int = 15_000) -> None:
    """Wait until the URL addresses the overlay that succeeded ``route``.

    The pre-S8 console addressed pages directly (``#/agents/ag-1``);
    the console addresses one workspace and hangs surfaces off it as
    ``#/w/<wid>?overlay=<name>[:<section>[:<id>]]``. A test that waited
    on the old shape waited forever, since nothing writes it any more.
    """
    target = overlay_target(route)
    try:
        page.wait_for_url(f"**overlay={target}**", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - re-raised with the URL
        raise AssertionError(
            f"never reached overlay={target!r} for route {route!r}.\n"
            f"  current url: {page.url}"
        ) from exc


def open_legacy_route(page: Page, console_url: str, route: str,
                      *, tab: str | None = None, wid: str | None = None,
                      timeout: int = 20_000):
    """Open the overlay that succeeded a legacy console route.

    ``wid`` is optional because these surfaces are platform-wide, not
    workspace-scoped: with none given the console resolves the first
    workspace itself and the overlay still opens, since the URL grammar
    parses the overlay independently of the workspace.
    """
    target = overlay_target(route)
    if tab:
        # A tabbed detail page states its tab in the section slot, which
        # is the same slot the router shim reads back as query.tab.
        head, _, rest = target.partition(":")
        record = rest.rpartition(":")[2] if rest else ""
        target = f"{head}:{tab}:{record}" if record else f"{head}:{tab}"
    name = target.split(":")[0]
    prefix = f"#/w/{wid}" if wid else "#/"
    page.goto(f"{console_url}{prefix}?overlay={target}")
    body = page.get_by_test_id("nv-overlay-body")
    expect(body).to_be_visible(timeout=timeout)
    expect(page.get_by_test_id(f"nv-overlay:{name}")).to_be_visible(timeout=timeout)
    return body


# ---------------------------------------------------------------------------
# Mobile shell (NV_MobileShell, US-014) navigation
# ---------------------------------------------------------------------------
#
# Below the mobile band (<=639px, useViewport's own threshold) the console
# swaps to an entirely different root component (nv-mobile-shell.jsx) that
# does not know the ?overlay= grammar: NV_OverlayHost only mounts inside the
# desktop branch, so open_legacy_route's nv-overlay-body wait can never
# resolve there. A platform overlay whose top-level name IS a mobile
# Platform nav id (see NV_PLAT_GROUPS) gets intercepted instead and lands on
# the More tab's fact-sheet flow (M5) - which is a real surface, but a
# read-only one (rows open a fact sheet; "Edit on desktop"). These helpers
# drive the mobile shell on its own terms.

MOBILE_TABS = ["inbox", "spaces", "files", "more"]


def open_mobile_shell(page: Page, console_url: str, *, timeout: int = 20_000) -> None:
    page.goto(f"{console_url}#/")
    expect(page.get_by_test_id("nv-mobile-shell")).to_be_visible(timeout=timeout)


def open_mobile_tab(page: Page, console_url: str, tab_id: str, *, timeout: int = 20_000):
    """Land on one of the mobile shell's bottom-nav tabs (Inbox / Spaces /
    Files / More) - the actual top-level surfaces a phone user reaches,
    replacing the desktop's ?overlay= grammar at this viewport band."""
    open_mobile_shell(page, console_url, timeout=timeout)
    page.get_by_role("tab", name=tab_id.capitalize()).click()
    panel = page.get_by_test_id(f"nv-mobile-panel:{tab_id}")
    expect(panel).to_be_visible(timeout=timeout)
    return panel


def open_mobile_platform_nav(page: Page, console_url: str, nav_id: str, *,
                              timeout: int = 20_000):
    """Drill into a Platform entity list from the More tab (NV_MobilePlatform)
    - the mobile-native replacement for a desktop Platform overlay, always
    rendered as .card rows (NV_MobilePlatform has no <table> branch), never
    the classic page component itself."""
    open_mobile_tab(page, console_url, "more", timeout=timeout)
    page.get_by_test_id(f"nv-mob-plat-nav:{nav_id}").click()
    rows = page.get_by_test_id(f"nv-mob-plat-page:{nav_id}")
    expect(rows).to_be_visible(timeout=timeout)
    return rows
