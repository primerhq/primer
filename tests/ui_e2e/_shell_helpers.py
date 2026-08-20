"""Playwright helpers for the fresh shell (S8).

The facade amendment M16 asks for: every ui_e2e test written between S1
and S8 drives the console through a helper layer, so re-pointing at the
fresh shell is one edit here rather than N edits across the suite. P5
rewrites _studio_helpers.py to delegate to these functions.

Selectors mirror ui/components/shell/*.jsx exactly:

  shell-root / shell-topbar / shell-rail / shell-center / shell-statusbar
  shell-tab:<doc-id> · shell-overlay:<name> · shell-palette
  rail-sessions / rail-files / rail-attention · attention-item:<sid>
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
    expect(page.get_by_test_id("shell-root")).to_be_visible(timeout=timeout)


def open_doc(page: Page, console_url: str, wid: str, kind: str, ref: str,
             *, anchor: str | None = None, timeout: int = 45_000) -> None:
    """Deep-link a document open and wait for its tab.

    The budget is deliberately generous. Reaching a document in another
    workspace is a chain, not a navigation: the shell boots on whichever
    workspace it lands in first, may lazily create a session there,
    then follows the url to the target workspace, drops the previous
    workspace's tabs and opens this one. 20 s covered that on an idle
    runner and not on a busy one, which showed up as a tab that the
    failure screenshot then proved was present all along.
    """
    url = f"{console_url}#/w/{wid}?doc={kind}:{ref}"
    if anchor:
        url += f"#{anchor}"
    page.goto(url)
    expect(page.get_by_test_id(f"shell-tab:{kind}:{ref}")).to_be_visible(timeout=timeout)


def open_overlay(page: Page, console_url: str, wid: str, name: str,
                 *, timeout: int = 20_000) -> None:
    page.goto(f"{console_url}#/w/{wid}?overlay={name}")
    expect(page.get_by_test_id(f"shell-overlay:{name}")).to_be_visible(timeout=timeout)


def open_palette(page: Page) -> None:
    """Open the command palette, by its own affordance.

    Waits for the shell first: the chord is handled by a window keydown
    listener the palette installs on mount, so pressing it at a page
    that is still booting goes nowhere and the palette simply never
    appears.

    Clicks the chip rather than pressing Ctrl+K. The chip is the visible
    way in, so a caller checking that a surface is reachable without
    typing a URL is checking the path a person would actually take; the
    chord has its own coverage in tests/ui.
    """
    expect(page.get_by_test_id("shell-root")).to_be_visible(timeout=20_000)
    chip = page.get_by_test_id("shell-palette-chip")
    expect(chip).to_be_visible(timeout=20_000)
    chip.click()
    expect(page.get_by_test_id("shell-palette")).to_be_visible(timeout=10_000)


def run_verb(page: Page, label: str) -> None:
    open_palette(page)
    page.get_by_test_id("shell-palette-input").fill(label)
    page.get_by_test_id("shell-palette-row").first.click()


def session_row(page: Page, sid: str):
    return page.get_by_test_id(f"rail-session:{sid}")


def attention_item(page: Page, sid: str):
    return page.get_by_test_id(f"attention-item:{sid}")


# ---------------------------------------------------------------------------
# Legacy route translation
# ---------------------------------------------------------------------------
#
# Every management page the console used to serve at its own "#/<page>"
# route is an overlay on the shell. The e2e suite predates that move and
# navigates by page name, so translate here rather than in N tests: one
# table, one edit whenever a surface moves again.
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
    "subsystems/internal-collections": "collections",
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
    """Translate a legacy console route into a shell overlay target.

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
        f"no shell overlay for legacy route {route!r}; add it to "
        "LEGACY_ROUTE_OVERLAYS or drive the surface it moved to"
    )


def wait_for_overlay_url(page: Page, route: str, *,
                         timeout: int = 15_000) -> None:
    """Wait until the URL addresses the overlay that succeeded ``route``.

    The pre-S8 console addressed pages directly (``#/agents/ag-1``);
    the shell addresses one workspace and hangs surfaces off it as
    ``#/w/<wid>?overlay=<name>[:<section>[:<id>]]``. A test that waited
    on the old shape waited forever, since nothing writes it any more.

    Takes the same legacy route the rest of this module speaks, so a
    call site reads as what it is checking rather than as URL grammar.
    """
    target = overlay_target(route)
    try:
        page.wait_for_url(f"**overlay={target}**", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - re-raised with the URL
        # Playwright's timeout names the pattern but not what the URL
        # actually is, which is the one thing needed to tell "the app
        # navigated somewhere else" from "the app never navigated".
        raise AssertionError(
            f"never reached overlay={target!r} for route {route!r}.\n"
            f"  current url: {page.url}"
        ) from exc


def open_legacy_route(page: Page, console_url: str, route: str,
                      *, tab: str | None = None, wid: str | None = None,
                      timeout: int = 20_000):
    """Open the overlay that succeeded a legacy console route.

    ``wid`` is optional because these surfaces are platform-wide, not
    workspace-scoped: with none given the shell resolves the first
    workspace itself (SH_RootGate) and the overlay still opens, since
    the URL grammar parses the overlay independently of the workspace.
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
    body = page.get_by_test_id("shell-overlay-body")
    expect(body).to_be_visible(timeout=timeout)
    expect(page.get_by_test_id(f"shell-overlay:{name}")).to_be_visible(timeout=timeout)
    return body
