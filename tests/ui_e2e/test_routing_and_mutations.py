"""Routing surfaces + mutation-feedback flows that aren't covered by
the existing test_routing.py / test_agents_create.py / test_workspaces_*
modules.

Covers:
* U0023 — New-workspace modal: create → toast → navigate to detail.
* U0033 — Agent detail Config tab deep-link survives reload.
* U0039 — Agent detail "Back" button navigates to /agents.
* U0044 — Modal ESC keypress closes the open modal.
"""

from __future__ import annotations

import httpx


# ---------------------------------------------------------------------------
# Shared seeders — keep self-contained so the file is grep-readable.
# ---------------------------------------------------------------------------


def _seed_llm_provider(base_url: str, pid: str) -> None:
    """POST a placeholder ollama LLM provider so the agent seeder
    can reference a real id without calling an upstream model."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM provider failed: {r.text}"


def _seed_agent(base_url: str, agent_id: str, provider_id: str) -> None:
    """POST a minimal agent bound to the provider."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "ui-e2e probe",
            "model": {
                "provider_id": provider_id,
                "model_name": "fake-model",
            },
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# U0023 — New-workspace modal happy path
# ---------------------------------------------------------------------------


def test_u0023_new_workspace_modal_creates_row_toasts_and_navigates(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0023 — Open the Workspaces list, click "New workspace", pick
    the seeded template from the dropdown, submit. The modal must
    close, a success toast must appear, the URL must navigate to
    ``#/workspaces/<new-id>``, and the detail-page title must render
    the new id.

    Priority 1 — mutation feedback. Sister to U0006 (agents) for the
    workspace-create flow. The backend allocates the id (workspace
    spec §12 — user-supplied id silently ignored), so the test
    captures the id from the URL after navigation rather than
    predicting it.
    """
    wp_id = f"wp-u0023-{unique_suffix}"
    tpl_id = f"wt-u0023-{unique_suffix}"
    created_ws_id: str | None = None
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed provider failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "u0023 template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed template failed: {r.text}"

    try:
        page.goto(f"{console_url}#/workspaces", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Workspaces", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Open the modal — button label "New workspace" per
        # ui/components/workspaces.jsx:88.
        page.get_by_role("button", name="New workspace").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # The dropdown auto-selects the first template per
        # NewWorkspaceModal's useEffect (workspaces.jsx:196). Pin via
        # explicit selection so we don't depend on ordering.
        page.locator("select.select").first.select_option(value=tpl_id)

        # Submit.
        page.get_by_role("button", name="Create").first.click()

        # Wait for modal close + URL change to #/workspaces/<id>.
        # The new id is backend-allocated so we glob.
        page.wait_for_url(
            lambda url: "#/workspaces/" in url and not url.endswith(
                "#/workspaces"
            ),
            timeout=15_000,
        )
        # Capture the id from the URL.
        url = page.url
        # url is e.g. http://127.0.0.1:8765/console/#/workspaces/ws-XXXX
        created_ws_id = url.rsplit("/", 1)[-1].split("?")[0]
        assert created_ws_id.startswith("ws-"), (
            f"unexpected workspace id format in URL: {url}"
        )

        # Detail title carries the new id.
        page.locator("h1.page-title").get_by_text(
            created_ws_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Success toast: "Workspace created" (workspaces.jsx:155).
        page.get_by_text("Workspace created", exact=False).first.wait_for(
            state="visible", timeout=5_000,
        )
    finally:
        cleanup = []
        if created_ws_id:
            cleanup.append(f"/v1/workspaces/{created_ws_id}")
        cleanup.extend([
            f"/v1/workspace_templates/{tpl_id}",
            f"/v1/workspace_providers/{wp_id}",
        ])
        _cleanup(base_url, cleanup)


# ---------------------------------------------------------------------------
# U0033 — Agent Config tab deep-link survives reload
# ---------------------------------------------------------------------------


def test_u0033_agent_config_tab_deep_link_survives_reload(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0033 — Sister to U0018 (Tools tab) for the Config tab.
    Navigate to ``#/agents/<id>?tab=config``, confirm Config is
    selected, reload the page, confirm Config is still selected and
    the URL still carries ``?tab=config``.

    Priority 6 — routing. Pins that the routerQuery.tab handling is
    symmetric across all four AGENT_TABS, not just Tools.
    Re-validates against the documented contract at
    agents.jsx:363 (``AGENT_TABS.some(...) ? routerQuery.tab : "config"``).

    Config is the default tab — so this test also defends against a
    regression where the default-fallback path silently strips the
    query string on reload.
    """
    provider_id = f"llm-u0033-{unique_suffix}"
    agent_id = f"ag-u0033-{unique_suffix}"
    _seed_llm_provider(base_url, provider_id)
    _seed_agent(base_url, agent_id, provider_id)

    try:
        page.goto(
            f"{console_url}#/agents/{agent_id}?tab=config",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        config_tab = page.get_by_role("tab", name="Config").first
        config_tab.wait_for(state="visible", timeout=5_000)
        assert config_tab.get_attribute("aria-selected") == "true", (
            f"Config tab not selected on initial deep-link nav; "
            f"aria-selected={config_tab.get_attribute('aria-selected')!r}"
        )

        # Reload — URL query should survive.
        page.reload(wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        config_tab_after = page.get_by_role("tab", name="Config").first
        config_tab_after.wait_for(state="visible", timeout=5_000)
        assert config_tab_after.get_attribute("aria-selected") == "true", (
            f"Config tab lost selected state after reload; "
            f"aria-selected={config_tab_after.get_attribute('aria-selected')!r}"
        )

        # Defence: URL still has ?tab=config.
        assert "tab=config" in page.url, (
            f"reload dropped ?tab=config query: {page.url}"
        )
    finally:
        _cleanup(base_url, [
            f"/v1/agents/{agent_id}",
            f"/v1/llm_providers/{provider_id}",
        ])


# ---------------------------------------------------------------------------
# U0039 — Agent detail "Back" button navigates to /agents
# ---------------------------------------------------------------------------


def test_u0039_agent_detail_back_button_navigates_to_list(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0039 — On an agent detail page, clicking the "Back" button in
    the page actions returns the operator to ``#/agents`` with the
    seeded agent row visible.

    Priority 6 — routing. Pins the documented header affordance from
    agents.jsx:485. The crumb link "Agents" and the "Back" button
    both navigate to the same place; this test exercises the Back
    button specifically so a regression that hides or breaks it
    surfaces without false-passing on the crumb link.
    """
    provider_id = f"llm-u0039-{unique_suffix}"
    agent_id = f"ag-u0039-{unique_suffix}"
    _seed_llm_provider(base_url, provider_id)
    _seed_agent(base_url, agent_id, provider_id)

    try:
        page.goto(
            f"{console_url}#/agents/{agent_id}",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # The "Back" button lives in .page-actions per
        # AgentDetailHeader. There may be other buttons with similar
        # names elsewhere on the page; scope to the page header.
        header_actions = page.locator(".page-header .page-actions").first
        header_actions.wait_for(state="visible", timeout=5_000)
        header_actions.get_by_role("button", name="Back").first.click()

        # Confirm URL is now #/agents and the list re-rendered with
        # the seeded row.
        page.wait_for_url(lambda url: url.rstrip("/").endswith("#/agents"), timeout=10_000)
        page.locator("h1.page-title").get_by_text(
            "Agents", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        page.locator(f"tr:has-text('{agent_id}')").first.wait_for(
            state="visible", timeout=10_000,
        )
    finally:
        _cleanup(base_url, [
            f"/v1/agents/{agent_id}",
            f"/v1/llm_providers/{provider_id}",
        ])


# ---------------------------------------------------------------------------
# U0044 — Modal ESC keypress closes any open create modal
# ---------------------------------------------------------------------------


def test_u0044_modal_escape_keypress_closes_open_create_modal(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0044 — Open the Agents "New agent" modal and press Escape.
    The modal must be removed from the DOM and the underlying list
    page must remain visible.

    Priority 1 — keyboard-driven mutation feedback / accessibility.
    Pin the documented ESC handler at ui/components/shared.jsx:107
    (window-level keydown → onClose). A regression that loses this
    handler would otherwise be invisible to mouse-driven tests.

    Setup creates a single placeholder LLM provider so the New
    agent modal is in a non-empty state when it opens (the modal
    loads /llm_providers to populate the provider dropdown).
    """
    provider_id = f"llm-u0044-{unique_suffix}"
    _seed_llm_provider(base_url, provider_id)

    try:
        page.goto(f"{console_url}#/agents", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Agents", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Open the modal — same button label/path as U0006.
        page.get_by_role("button", name="New agent").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)
        # Sanity: there is exactly one modal in the DOM now.
        assert page.locator(".modal").count() == 1, (
            f"expected 1 modal after open; got "
            f"{page.locator('.modal').count()}"
        )

        # Press Escape (target body so the handler fires).
        page.keyboard.press("Escape")

        # Modal must be removed from the DOM. wait_for state hidden
        # is the canonical Playwright check.
        modal.wait_for(state="hidden", timeout=5_000)
        assert page.locator(".modal").count() == 0, (
            f"modal still in DOM after ESC; count="
            f"{page.locator('.modal').count()}"
        )

        # Defence: underlying list page is still rendered.
        page.locator("h1.page-title").get_by_text(
            "Agents", exact=False,
        ).first.wait_for(state="visible", timeout=5_000)
    finally:
        _cleanup(base_url, [
            f"/v1/llm_providers/{provider_id}",
        ])
