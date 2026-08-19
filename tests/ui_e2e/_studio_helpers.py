"""Shared Playwright helpers for driving the workspace console.

S8 replaced the Studio with the fresh shell (``ui/components/shell/*.jsx``).
This module keeps its NAME and its function signatures so the e2e suite
written between S1 and S8 did not need N edits when the surface changed:
that is exactly what facade amendment M16 asked for. Every body here now
delegates to :mod:`tests.ui_e2e._shell_helpers`.

The shell's model differs from the Studio's in three ways the helpers
have to bridge:

* One workspace URL (``#/w/<wid>``) rather than ``#/workspaces/<wid>``.
* Documents, not panels: a session opens as a TAB
  (``shell-tab:session:<sid>``) whose body is one uniform session
  document for every binding kind. There is no agent/graph panel split,
  so the ``kind`` argument is accepted and ignored.
* Attention is always mounted (``rail-attention``) instead of hiding
  behind a debug toggle, so "expand the panel" becomes an assertion.

Selectors mirror the shell components exactly; see ``_shell_helpers``.

Nothing here starts a server -- that is the harness's job (see the module
docstring in ``conftest.py``).
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import (
    SHELL_CONSOLE_IGNORES,
    open_doc,
    open_overlay,
    open_palette,
    open_shell,
    run_verb,
    shell_url,
)

__all__ = [
    "STUDIO_CONSOLE_IGNORES",
    "action_item_for_session",
    "expand_debug_sidebar",
    "files_list",
    "is_studio_v2",
    "kind_text",
    "open_palette",
    "open_provider_catalog",
    "open_session_in_studio",
    "open_session_via_sidebar",
    "open_studio",
    "open_workspace_settings",
    "run_verb",
    "session_row",
    "sessions_list",
    "studio_url",
]

# The shell polls GET /v1/internal_collections/config, which 404s whenever
# the Internal Collections feature is inactive (the common e2e default).
# That is a pre-existing app 404, not a shell bug -- allowlist it wherever
# a test asserts a clean console.
STUDIO_CONSOLE_IGNORES = [
    *SHELL_CONSOLE_IGNORES,
    r"/v1/internal_collections/config",
    r"internal_collections/config",
    r"Failed to load resource:.*status of 404",
]


def session_row(page: Page, sid: str):
    """The rail row for a specific session id.

    The row renders the session TITLE (binding name or a truncated id),
    NOT the raw ``sess-<id>``, so filtering by ``has_text`` is unreliable;
    the rail stamps the id into the testid instead. The rail is
    per-workspace, so navigate to the session's OWN workspace first.
    """
    return page.get_by_test_id(f"rail-session:{sid}")


def studio_url(console_url: str, wid: str) -> str:
    """The console URL for a workspace (``#/w/<wid>``)."""
    return shell_url(console_url, wid)


def is_studio_v2(page: Page) -> bool:
    """True once the shell has mounted.

    The Studio era ran two shells behind a runtime tweak and callers had
    to ask which one they got. Only one shell ships now, so this waits
    for it rather than choosing between two vocabularies.
    """
    page.get_by_test_id("shell-root").wait_for(state="attached", timeout=20_000)
    return True


# The shell states what an item wants in operator copy: a pending
# ask_user reads "Question", a pending approval reads "Approve <tool>".
# Journeys assert an item identifies its kind, not which vocabulary
# happens to be shipped, so they ask here.
_SHELL_KIND_COPY = {
    "ask_user": "Question",
    "approval": "Approve",
    "ask_approval": "Approve",
}


def kind_text(page: Page, kind: str) -> str:
    """The text an attention item renders for a park ``kind``."""
    return _SHELL_KIND_COPY.get(kind, kind)


def sessions_list(page: Page):
    """The rail's session list."""
    return page.get_by_test_id("rail-sessions")


def files_list(page: Page, *, timeout: int = 10_000):
    """The rail's file tree."""
    files = page.get_by_test_id("rail-files")
    expect(files).to_be_visible(timeout=timeout)
    return files


def open_studio(page: Page, console_url: str, wid: str, *, timeout: int = 20_000) -> None:
    """Navigate to a workspace and wait for the shell to mount.

    Confirms the region wrappers render so callers can immediately reach
    the rail and the center. Attention is part of the rail and is mounted
    even when empty -- "nothing needs you" is a state worth showing.
    """
    open_shell(page, console_url, wid, timeout=timeout)
    for region in ("shell-rail", "shell-center"):
        expect(page.get_by_test_id(region)).to_be_visible(timeout=10_000)


def open_session_in_studio(
    page: Page,
    console_url: str,
    wid: str,
    sid: str,
    *,
    kind: str = "agent",
    timeout: int = 20_000,
) -> None:
    """Deep-link a session open as a tab in its workspace.

    ``kind`` is accepted for call-site compatibility and ignored: the
    shell renders ONE session document for every binding kind, so there
    is no agent/graph panel to wait on. The tab's presence IS the wait.
    """
    del kind
    open_doc(page, console_url, wid, "session", sid, timeout=timeout)
    expect(page.get_by_test_id(f"shell-session:{sid}")).to_be_visible(timeout=timeout)


def open_session_via_sidebar(
    page: Page,
    console_url: str,
    wid: str,
    *,
    kind: str = "agent",
    timeout: int = 20_000,
):
    """Open a session by CLICKING its rail row.

    Returns the clicked row locator. Use this (rather than the deep-link)
    when the test's intent is the rail-list to tab interaction itself.
    """
    del kind
    open_studio(page, console_url, wid, timeout=timeout)
    rows = page.locator('[data-testid^="rail-session:"]')
    row = rows.first
    expect(row).to_be_visible(timeout=timeout)
    row.click()
    expect(page.locator('[data-testid^="shell-tab:session:"]').first).to_be_visible(
        timeout=timeout
    )
    return row


def open_workspace_settings(
    page: Page,
    console_url: str,
    wid: str,
    section: str,
    *,
    timeout: int = 20_000,
):
    """Open a workspace's own settings tabs and select a section.

    ``section`` is one of ``files`` / ``sessions`` / ``events`` / ``log``
    / ``channels`` / ``config`` / ``destroy``. The shell hosts these in
    the ``workspaces`` overlay's ``detail`` section, which re-uses the
    exact WorkspaceDetail panels (WS_ChannelsTab / WS_ConfigTab /
    WS_LogTab / WS_DestroyTab), so a caller keeps its existing
    label/role assertions on the returned scope.

    Returns the overlay body locator so callers can scope subsequent
    queries inside it (avoiding strict-mode clashes with the nested
    Link-channel / Destroy-confirm modals rendered on top).
    """
    page.goto(f"{console_url}#/w/{wid}?overlay=workspaces:detail:{wid}")
    body = page.get_by_test_id("shell-overlay-body")
    expect(body).to_be_visible(timeout=timeout)
    tab = page.get_by_test_id(f"workspace-tab:{section}")
    expect(tab).to_be_visible(timeout=timeout)
    tab.click()
    return body


def expand_debug_sidebar(page: Page, *, timeout: int = 10_000) -> None:
    """Assert the attention list is present.

    Nothing to expand: attention is mounted in the rail at all times,
    including when empty. That IS the shell's premise, so the Studio's
    open-the-panel-first step becomes a visibility assertion.
    """
    expect(page.get_by_test_id("rail-attention")).to_be_visible(timeout=timeout)


def action_item_for_session(page: Page, sid: str):
    """The attention item for a session id.

    ask_user / approval parks surface in the rail's attention list, one
    item per pending yield, keyed by session so the match stays scoped to
    THIS session even when the shared DB left other parks around.
    """
    return page.get_by_test_id(f"attention-item:{sid}")


def open_provider_catalog(
    page: Page,
    console_url: str,
    *,
    wid: str | None = None,
    cls: str | None = None,
    instance_id: str | None = None,
    via: str = "url",
    timeout: int = 20_000,
):
    """Open the unified provider catalog and wait for its body.

    S8 re-hosts the catalog as an overlay, so THIS is the only navigation
    site that changed. ``via="url"`` deep-links it
    (``?overlay=providers[:<class>[:<id>]]``); ``via="palette"`` runs the
    catalog's own verb instead, which is the reachability path -- an
    overlay only the URL can reach is one a user cannot find.

    ``wid`` defaults to the workspace already open, which is what a test
    that only cares about the catalog wants.
    """
    if via == "url":
        target = "providers"
        if cls:
            target += ":" + cls
            if instance_id:
                target += ":" + instance_id
        if wid:
            page.goto(f"{console_url}#/w/{wid}?overlay={target}")
        else:
            page.goto(f"{console_url}#/w/?overlay={target}")
    else:
        run_verb(page, "Open Providers Catalog")
    body = page.get_by_test_id("shell-overlay-body")
    expect(body).to_be_visible(timeout=timeout)
    return body
