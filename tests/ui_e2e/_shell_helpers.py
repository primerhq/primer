"""Playwright helpers for the three-view console (flag day P7).

The facade amendment M16 asks for: every ui_e2e test drives the console
through a helper layer, so re-pointing at a new shell is one edit here
rather than N edits across the suite. That bet paid out twice now -
first S8's fresh shell, now the three-view console.

Selectors mirror ui/components/console/*.jsx exactly:

  nv-root / nv-topbar / nv-actbar / nv-dochost
  nv-tab:<kind>:<ref> · nv-overlay:<name> · nv-palette
  nv-sessions / nv-files · nv-session:<sid> (band row)
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
    expect(page.get_by_test_id(f"nv-tab:{kind}:{ref}")).to_be_visible(timeout=timeout)


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


def session_row(page: Page, sid: str):
    return page.get_by_test_id(f"nv-session:{sid}")


def attention_item(page: Page, sid: str):
    """The attention affordance for a session.

    The S8 toast/inbox tier died with the flag day: attention is the
    "Needs you" band row (attention-dotted), which opens the session
    whose inline card carries the decision.
    """
    return page.get_by_test_id(f"nv-session:{sid}")


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
