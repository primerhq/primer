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
#
# NB: ``assert_no_console_errors`` matches each pattern against the console
# message TEXT (``m["text"]``), not its ``location.url``. Chromium's 404
# console line is the URL-less
#   "Failed to load resource: the server responded with a status of 404 ..."
# so the ``internal_collections/config`` URL patterns below would never match
# on their own — the "status of 404" text pattern is the one that actually
# suppresses this app-shell 404. (The URL patterns are kept for callers that
# inspect ``failed_requests[*].url`` instead.)
STUDIO_CONSOLE_IGNORES = [
    r"net::ERR_ABORTED",
    r"favicon",
    r"/v1/internal_collections/config",
    r"internal_collections/config",
    r"Failed to load resource:.*status of 404",
]


def session_row(page: Page, sid: str):
    """Locate the Studio sidebar ``session-row`` for a specific session id.

    The row renders the session TITLE (agent/binding name or a truncated
    id), NOT the raw ``sess-<id>``, so filtering by ``has_text`` is
    unreliable. studio-sidebar.jsx stamps ``data-session-id`` on each row so
    e2e can locate a session deterministically while the visible title stays
    unchanged. The sidebar is per-workspace, so navigate to the session's
    OWN workspace Studio (``#/workspaces/<wid>``) before using this.
    """
    return page.locator(
        f'[data-testid="session-row"][data-session-id="{sid}"]'
    )


def studio_url(console_url: str, wid: str) -> str:
    """The Studio route for a workspace (``#/workspaces/<wid>``)."""
    return f"{console_url}#/workspaces/{wid}"


def is_studio_v2(page: Page) -> bool:
    """True when the page is showing the revamped Studio shell.

    Detected from the DOM rather than from the tweak default, so these helpers
    keep working for whichever shell is actually rendered - the flag is a
    runtime tweak, so one build serves both and a test may pin either.
    ``studio-rail`` exists only in the revamp; ``studio-sidebar-inner`` only in
    the v1 sidebar. Waits for whichever appears first, because counting before
    the shell has mounted answers "not v2" for both shells - which would send a
    v2 caller looking for v1 testids that will never arrive.
    """
    either = page.locator(
        '[data-testid="studio-rail"], [data-testid="studio-sidebar-inner"]'
    )
    either.first.wait_for(state="attached", timeout=20_000)
    return page.locator('[data-testid="studio-rail"]').count() > 0


# The revamp replaced raw park kinds with copy an operator can read: an item
# says "asked a question", not "ask_user". Journeys assert the item identifies
# its kind, not which vocabulary happens to be shipped, so they ask here.
_V2_KIND_COPY = {
    "ask_user": "asked a question",
    "approval": "wants approval",
    "ask_approval": "wants approval",
    "watch_files": "waiting on a file",
    "sleep": "sleeping",
}


def show_all_action_items(page: Page, *, timeout: int = 10_000) -> None:
    """Make every pending action item reachable, on either shell.

    v1 listed all of them in the right rail at once. The revamp shows the
    oldest in the always-visible bar and puts the rest behind an inbox - one
    thing needing you is the common case, and a rail-length list of them is
    not something to render permanently. Any journey that asserts across more
    than one pending item has to open the inbox first; with one item, the bar
    alone is enough and this is still safe to call.
    """
    if not is_studio_v2(page):
        return
    # Wait for the bar to actually have something in it first. The pending
    # snapshot arrives after mount, so the bar is briefly in its calm state -
    # and the calm bar has no inbox, so checking too early finds nothing, skips
    # silently, and leaves every item past the first unreachable.
    page.locator("[data-testid='action-item']").first.wait_for(
        state="attached", timeout=timeout,
    )
    inbox = page.locator("[data-testid='attention-inbox']")
    if inbox.count() and inbox.get_attribute("aria-expanded") != "true":
        inbox.click()
        expect(page.locator("[data-testid='attention-queue']")).to_be_visible(timeout=timeout)


def kind_text(page: Page, kind: str) -> str:
    """The text an action item renders for a park ``kind`` on this shell."""
    return _V2_KIND_COPY.get(kind, kind) if is_studio_v2(page) else kind


def sessions_list(page: Page):
    """The list of session rows, whichever shell is rendered.

    v1 stacked a ``sessions-section`` above a ``files-section``; the revamp
    replaces both with one rail in two modes, so the runs list is ``rail-runs``.
    """
    return page.locator(
        '[data-testid="rail-runs"]' if is_studio_v2(page) else '[data-testid="sessions-section"]'
    )


def files_list(page: Page, *, timeout: int = 10_000):
    """The file tree, switching the rail into Files mode first when needed.

    On v1 the tree is always mounted below the sessions section. In the revamp
    Files is one of two rail modes and Runs is the default, so a caller that
    wants the tree has to ask for it - which is the trade the single rail makes
    for giving whichever list you are using the full column height.
    """
    if not is_studio_v2(page):
        return page.locator('[data-testid="files-section"]')
    rail_files = page.locator('[data-testid="rail-files"]')
    if rail_files.count() == 0:
        page.locator('[data-testid="rail-mode-files"]').click()
    expect(rail_files).to_be_visible(timeout=timeout)
    return rail_files


def open_studio(page: Page, console_url: str, wid: str, *, timeout: int = 20_000) -> None:
    """Navigate to a workspace's Studio and wait for the shell to mount.

    Confirms the region wrappers render so callers can immediately reach the
    left rail and the center.

    studioV2 has no right column: Action Required moved into the always-mounted
    attention bar and the event tap into the investigate dock, so
    ``studio-activity`` is asserted only on the v1 shell (see
    ``expand_debug_sidebar``).
    """
    page.goto(studio_url(console_url, wid), wait_until="domcontentloaded")
    expect(page.locator('[data-testid="studio-root"]')).to_be_visible(timeout=timeout)
    for region in ("studio-sidebar", "studio-center"):
        expect(page.locator(f'[data-testid="{region}"]')).to_be_visible(timeout=10_000)
    if is_studio_v2(page):
        expect(page.locator('[data-testid="attention-bar"]')).to_be_visible(timeout=10_000)
    else:
        expect(page.locator('[data-testid="studio-activity"]')).to_be_visible(timeout=10_000)


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


def expand_debug_sidebar(page: Page, *, timeout: int = 10_000) -> None:
    """Expand the Studio's right-sidebar workspace-events panel.

    The panel (``ActionRequired`` list — ``action-item`` /
    ``action-required-count`` / approval + ask_user controls — and the
    ``WorkspaceActivity`` feed) starts CLOSED by default (``debugOpen: false``);
    the right column is 0-width until opened. It is opened from the prominent
    ``studio-debug-toggle`` button in the studio HEADER (the same show/hide
    pattern as the terminal toggle — the earlier edge-rail affordance proved
    unclickable). Opening flips ``debugOpen`` so the column expands and the
    ``debug-sidebar-body`` becomes visible (both children stay mounted so their
    poll timers never reset). Any test that asserts on content inside that body
    must call this first — right after ``open_studio`` /
    ``open_session_in_studio`` / ``open_session_via_sidebar``.
    """
    if is_studio_v2(page):
        # Nothing to expand: the attention bar is mounted between the header
        # and the body at all times, including when empty. That IS the revamp's
        # premise - "nothing needs you" is a state worth showing - so the v1
        # open-the-panel-first step becomes a visibility assertion.
        expect(page.locator("[data-testid='attention-bar']")).to_be_visible(timeout=timeout)
        return
    toggle = page.locator("[data-testid='studio-debug-toggle']")
    expect(toggle).to_be_visible(timeout=timeout)
    if toggle.get_attribute("aria-pressed") != "true":
        toggle.click()
    expect(page.locator("[data-testid='debug-sidebar-body']")).to_be_visible(timeout=timeout)


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
