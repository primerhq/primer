"""Shared Playwright helpers for driving the workspace Studio.

The Studio (``ui/components/studio.jsx`` + ``studio-{sidebar,center,
activity,settings,palette}.jsx``) replaced the three retired UIs:

* the global ``#/sessions`` LIST page,
* the ``#/sessions/:id`` session-detail page (now a redirect),
* the ``#/workspaces/:id/:tab`` workspace-detail tabs (channels / config /
  git-log / destroy → a Studio **Settings** modal).

A session now opens as a center *tab* inside the workspace's Studio; the
management tabs live behind the sub-header gear. These helpers DRY the
navigation the re-pointed e2e tests share so each test can focus on its
own assertion rather than re-deriving the deep-link / modal dance.

Selectors (data-testids) mirror the Studio components exactly:

  studio-root / studio-sidebar / studio-center / studio-activity
  session-row · center-tab · panel-agent · panel-graph · panel-file
  studio-settings-btn · workspace-settings · workspace-settings-nav:<id>
  action-required / action-required-list / action-item

Nothing here starts a server — that is the harness's job (see the module
docstring in ``conftest.py``).
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# The app-shell (chrome.jsx Topbar) fetches GET /v1/internal_collections/config
# on every page, and that endpoint 404s whenever the Internal Collections
# feature is inactive (the common e2e default). The Studio renders IN-SHELL,
# so this pre-existing app-shell 404 is now visible to any Studio test's
# console-error assertion. It is NOT a Studio bug — allowlist it wherever a
# Studio test asserts a clean console.
STUDIO_CONSOLE_IGNORES = [
    r"net::ERR_ABORTED",
    r"favicon",
    r"/v1/internal_collections/config",
    r"internal_collections/config",
]


def studio_url(console_url: str, wid: str) -> str:
    """The Studio route for a workspace (``#/workspaces/<wid>``)."""
    return f"{console_url}#/workspaces/{wid}"


def open_studio(page: Page, console_url: str, wid: str, *, timeout: int = 20_000) -> None:
    """Navigate to a workspace's Studio and wait for the shell to mount.

    Confirms all three region wrappers render so callers can immediately
    reach the sidebar / center / activity columns.
    """
    page.goto(studio_url(console_url, wid), wait_until="domcontentloaded")
    expect(page.locator('[data-testid="studio-root"]')).to_be_visible(timeout=timeout)
    for region in ("studio-sidebar", "studio-center", "studio-activity"):
        expect(page.locator(f'[data-testid="{region}"]')).to_be_visible(timeout=10_000)


def open_session_in_studio(
    page: Page,
    console_url: str,
    wid: str,
    sid: str,
    *,
    kind: str = "agent",
    timeout: int = 20_000,
) -> None:
    """Deep-link a session open inside its workspace's Studio.

    Uses the ``?open=session:<sid>`` deep-link (studio.jsx ST_tabFromUrl →
    ST_applyUrlTab auto-opens + activates the tab on mount), then waits for
    the center tab plus the resolved panel:

    * ``kind="agent"`` → ``panel-agent`` (reused ``SessionLiveStream``)
    * ``kind="graph"`` → ``panel-graph`` (reused ``SD_GraphRunView``)

    The panel resolver (ST_SessionPanel) fetches GET /v1/sessions/<sid> and
    branches on ``binding.kind``; a graph session always lands on
    ``panel-graph`` regardless of the hint, but the hint lets a caller wait
    on the right panel deterministically.
    """
    page.goto(
        f"{console_url}#/workspaces/{wid}?open=session:{sid}",
        wait_until="domcontentloaded",
    )
    expect(page.locator('[data-testid="studio-root"]')).to_be_visible(timeout=timeout)
    expect(page.locator('[data-testid="center-tab"]').first).to_be_visible(timeout=timeout)
    panel = "panel-graph" if kind == "graph" else "panel-agent"
    expect(page.locator(f'[data-testid="{panel}"]')).to_be_visible(timeout=timeout)


def open_session_via_sidebar(
    page: Page,
    console_url: str,
    wid: str,
    *,
    kind: str = "agent",
    timeout: int = 20_000,
):
    """Open a session by CLICKING the first sidebar ``session-row``.

    Returns the clicked row locator. Use this (rather than the deep-link)
    when the test's intent is the sidebar-list → center-tab interaction
    itself. Waits for the resolved panel to render.
    """
    open_studio(page, console_url, wid, timeout=timeout)
    row = page.locator('[data-testid="session-row"]').first
    expect(row).to_be_visible(timeout=timeout)
    row.click()
    expect(page.locator('[data-testid="center-tab"]').first).to_be_visible(timeout=timeout)
    panel = "panel-graph" if kind == "graph" else "panel-agent"
    expect(page.locator(f'[data-testid="{panel}"]')).to_be_visible(timeout=timeout)
    return row


def open_workspace_settings(
    page: Page,
    console_url: str,
    wid: str,
    section: str,
    *,
    timeout: int = 20_000,
):
    """Enter a workspace's Studio, open the Settings modal, select a section.

    ``section`` is one of ``channels`` / ``config`` / ``log`` / ``destroy``
    (the left-rail nav ids in studio-settings.jsx). The modal re-uses the
    exact WorkspaceDetail panels (WS_ChannelsTab / WS_ConfigTab / WS_LogTab /
    WS_DestroyTab), so a caller keeps its existing label/role assertions on
    the returned panel scope.

    Returns the ``workspace-settings`` modal locator so callers can scope
    subsequent queries inside it (avoiding strict-mode clashes with the
    nested Link-channel / Destroy-confirm modals rendered on top).
    """
    open_studio(page, console_url, wid, timeout=timeout)
    gear = page.locator('[data-testid="studio-settings-btn"]')
    expect(gear).to_be_visible(timeout=timeout)
    gear.click()
    modal = page.locator('[data-testid="workspace-settings"]')
    expect(modal).to_be_visible(timeout=timeout)
    nav = page.locator(f'[data-testid="workspace-settings-nav:{section}"]')
    expect(nav).to_be_visible(timeout=timeout)
    nav.click()
    return modal


def action_item_for_session(page: Page, sid: str):
    """Locate the right-sidebar ``action-item`` for a session id.

    ask_user / approvals / watch / sleep parks surface in the RIGHT sidebar
    ``action-required`` list (StudioActivity → ActionRequired), one
    ``action-item`` per pending yield. The item carries a
    ``action-session-link`` button whose text is the (shortened) session id;
    filtering the list on that keeps the match scoped to THIS session even
    when the shared DB left other parks around.
    """
    return page.locator('[data-testid="action-item"]').filter(
        has=page.locator('[data-testid="action-session-link"]')
    )
