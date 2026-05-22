"""List-page filter behaviour + workspaces sidebar polling cadence +
Metadata tab deep-link preservation.

Covers:
* U0024 — Workspaces sidebar count polls within ~12s of API
  workspace create.
* U0034 — Agent detail Metadata tab deep-link survives reload.
* U0037 — Agents list filter input narrows the table to matching ids.
"""

from __future__ import annotations

import time

import httpx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"


def _seed_agent(base_url: str, agent_id: str, provider_id: str) -> None:
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
# U0024 — Workspaces sidebar count polls after API workspace create
# ---------------------------------------------------------------------------


def test_u0024_workspaces_sidebar_count_polls_after_api_create(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0024 — Seed a workspace_provider + workspace_template; open
    the dashboard; capture the baseline Workspaces sidebar count;
    POST a workspace via API; assert the sidebar count catches up
    to baseline+1 within one polling interval (~5s real cadence;
    we budget 15s for first-render + react batching).

    Priority 4 — polling cadence. Sister of U0002 (sessions) but
    on the Workspaces nav row. The /workspaces poll fires every
    5000 ms (chrome.jsx:111). The total comes from the response's
    ``total`` field — so the sidebar count reflects the global
    workspace count, not per-status sub-counts.
    """
    wp_id = f"wp-u0024-{unique_suffix}"
    tpl_id = f"wt-u0024-{unique_suffix}"
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
            "description": "u0024 template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed template failed: {r.text}"

    try:
        page.goto(f"{console_url}#/", wait_until="domcontentloaded")
        workspaces_nav = page.locator(
            ".nav-item:has(.label:text('Workspaces'))"
        ).first
        workspaces_nav.wait_for(state="visible", timeout=10_000)

        def _read_count() -> int | None:
            count_el = workspaces_nav.locator(".count").first
            if count_el.count() == 0:
                return None
            txt = (count_el.text_content() or "").strip()
            try:
                return int(txt)
            except ValueError:
                return None

        # Capture baseline (could be 0 — that's fine).
        baseline: int | None = None
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            baseline = _read_count()
            if baseline is not None:
                break
            page.wait_for_timeout(250)
        assert baseline is not None, (
            "Workspaces sidebar count never rendered within 12s — "
            "polls aren't loading at all on the freshly opened page"
        )

        # POST the workspace via API.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post("/v1/workspaces", json={"template_id": tpl_id})
            assert r.status_code == 201, f"seed workspace failed: {r.text}"
            created_ws_id = r.json()["id"]

        # Wait for the sidebar count to catch up to baseline+1.
        target = baseline + 1
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            now = _read_count()
            if now is not None and now >= target:
                break
            page.wait_for_timeout(250)
        final = _read_count()
        assert final is not None and final >= target, (
            f"Workspaces sidebar count did not catch up to API state "
            f"within 15s: baseline={baseline} expected≥{target} "
            f"final={final}"
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
# U0034 — Agent Metadata tab deep-link survives reload
# ---------------------------------------------------------------------------


def test_u0034_agent_metadata_tab_deep_link_survives_reload(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0034 — Sister of U0018 (Tools) and U0033 (Config) for the
    Metadata tab. Navigate to ``#/agents/<id>?tab=metadata``,
    confirm Metadata is selected, reload, confirm the URL query
    survives and Metadata is still selected.

    Priority 6 — routing. Pins that the routerQuery.tab handling
    is symmetric across all four AGENT_TABS (agents.jsx:352-357).
    Metadata is the last tab in the array — a regression that
    silently dropped it from AGENT_TABS would fall back to the
    "config" default on reload and surface here.
    """
    provider_id = f"llm-u0034-{unique_suffix}"
    agent_id = f"ag-u0034-{unique_suffix}"
    _seed_llm_provider(base_url, provider_id)
    _seed_agent(base_url, agent_id, provider_id)

    try:
        page.goto(
            f"{console_url}#/agents/{agent_id}?tab=metadata",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        metadata_tab = page.get_by_role("tab", name="Metadata").first
        metadata_tab.wait_for(state="visible", timeout=5_000)
        assert metadata_tab.get_attribute("aria-selected") == "true", (
            f"Metadata tab not selected on initial deep-link nav; "
            f"aria-selected={metadata_tab.get_attribute('aria-selected')!r}"
        )

        page.reload(wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        metadata_tab_after = page.get_by_role("tab", name="Metadata").first
        metadata_tab_after.wait_for(state="visible", timeout=5_000)
        assert metadata_tab_after.get_attribute("aria-selected") == "true", (
            f"Metadata tab lost selected state after reload; "
            f"aria-selected={metadata_tab_after.get_attribute('aria-selected')!r}"
        )

        # Defence: URL still has ?tab=metadata.
        assert "tab=metadata" in page.url, (
            f"reload dropped ?tab=metadata query: {page.url}"
        )
    finally:
        _cleanup(base_url, [
            f"/v1/agents/{agent_id}",
            f"/v1/llm_providers/{provider_id}",
        ])


# ---------------------------------------------------------------------------
# U0037 — Agents list filter narrows the table
# ---------------------------------------------------------------------------


def test_u0037_agents_list_filter_narrows_table_to_matching_ids(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0037 — Seed three agents with distinguishable ids via API,
    open the Agents list, type a unique substring of ONE agent's id
    into the filter input, and assert only the matching row remains
    visible while the other two are absent from the DOM.

    Priority 6 — list-page filter UX. Pins the substring-match
    contract from agents.jsx:44-48 (filter applies to id +
    description; case-insensitive substring). The agent ids are
    crafted to share the test suffix (so they're all from this
    test) but differ in a leading discriminator so a substring
    can target exactly one.
    """
    provider_id = f"llm-u0037-{unique_suffix}"
    # Discriminators: each id has a unique-to-this-test prefix
    # token. Filtering for the token selects exactly one row.
    agent_a = f"ag-u0037-alpha-{unique_suffix}"
    agent_b = f"ag-u0037-beta-{unique_suffix}"
    agent_c = f"ag-u0037-gamma-{unique_suffix}"
    _seed_llm_provider(base_url, provider_id)
    _seed_agent(base_url, agent_a, provider_id)
    _seed_agent(base_url, agent_b, provider_id)
    _seed_agent(base_url, agent_c, provider_id)

    try:
        page.goto(f"{console_url}#/agents", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Agents", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Wait for at least all three rows to appear before filtering
        # — defends against typing into the filter while the list is
        # mid-load (which would race the filter render).
        for aid in (agent_a, agent_b, agent_c):
            page.locator(f"tr:has-text('{aid}')").first.wait_for(
                state="visible", timeout=10_000,
            )

        # Type a substring unique to agent_b ("beta") into the filter.
        filter_input = page.get_by_placeholder("Filter agents", exact=False).first
        filter_input.wait_for(state="visible", timeout=5_000)
        filter_input.fill("beta")

        # agent_b row should still be visible.
        page.locator(f"tr:has-text('{agent_b}')").first.wait_for(
            state="visible", timeout=5_000,
        )

        # agent_a and agent_c rows should be gone from the DOM.
        # The filter rewrites `filtered` (agents.jsx:44) so non-
        # matching rows are not rendered at all.
        assert page.locator(f"tr:has-text('{agent_a}')").count() == 0, (
            f"agent_a row still rendered after filter='beta': "
            f"count={page.locator(f'tr:has-text({agent_a!r})').count()}"
        )
        assert page.locator(f"tr:has-text('{agent_c}')").count() == 0, (
            f"agent_c row still rendered after filter='beta': "
            f"count={page.locator(f'tr:has-text({agent_c!r})').count()}"
        )

        # Defence: clearing the filter restores all rows.
        filter_input.fill("")
        for aid in (agent_a, agent_b, agent_c):
            page.locator(f"tr:has-text('{aid}')").first.wait_for(
                state="visible", timeout=5_000,
            )
    finally:
        _cleanup(base_url, [
            f"/v1/agents/{agent_a}",
            f"/v1/agents/{agent_b}",
            f"/v1/agents/{agent_c}",
            f"/v1/llm_providers/{provider_id}",
        ])
