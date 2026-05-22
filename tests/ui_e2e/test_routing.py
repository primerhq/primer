"""Routing-surface regression tests.

The matrix console runs on a hash router (ui/foundation/router.js).
Hash changes flow through ``hashchange`` events, so browser back/forward
work natively. This module pins the user-visible contract: navigation
into and out of a detail page must always return the operator to the
same list state, and the command palette (Ctrl+K) must navigate to the
chosen page.

Covers:
* U0019 — browser back returns from agent detail to the agents list
  with no console errors (priority 6, routing).
* U0021 — Ctrl+K opens the command palette and typing + Enter
  navigates to the chosen route (priority — polish/keyboard).
"""

from __future__ import annotations

import httpx


def test_u0019_browser_back_returns_to_agents_list_no_errors(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    console_messages,
    assert_no_console_errors_fn,
) -> None:
    """U0019 — Seed an agent via API; navigate to /agents (list);
    click the row to drill into /agents/{id}; press the browser back
    button; the operator returns to /agents and the list re-renders
    with no console errors.

    Priority 6 — routing. The hash router (ui/foundation/router.js)
    delegates navigation to ``window.location.hash``; back/forward
    fire ``hashchange`` and the route resolver re-renders. The
    contract this test pins is the user-visible one: after going
    back, you see the agents list page (h1.page-title says "Agents",
    a row for the seeded id is visible, the URL hash is ``#/agents``)
    and the console didn't error on the round-trip.
    """
    provider_id = f"llm-u0019-{unique_suffix}"
    agent_id = f"ag-u0019-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "u0019 back-nav probe",
            "model": {
                "provider_id": provider_id,
                "model_name": "fake-model",
            },
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"

    try:
        # 1. Navigate to /agents list and confirm the seeded row.
        page.goto(f"{console_url}#/agents", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Agents", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        # The row uses the agent id as visible text; find it inside a
        # table cell so we don't match the title.
        agent_cell = page.locator(f"tr:has-text('{agent_id}')").first
        agent_cell.wait_for(state="visible", timeout=10_000)

        # 2. Click the row to drill into detail.
        agent_cell.click()
        page.locator("h1.page-title").get_by_text(
            agent_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        assert f"#/agents/{agent_id}" in page.url, (
            f"row click didn't navigate to detail: {page.url}"
        )

        # 3. Press browser back. Use page.go_back which dispatches a
        # real navigation (and the hash change that triggers React's
        # router subscription).
        page.go_back(wait_until="domcontentloaded")

        # 4. We must be back on the /agents list. Check both the URL
        # and the rendered title — URL alone is not enough because a
        # blank page would also have the right URL.
        page.locator("h1.page-title").get_by_text(
            "Agents", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        assert page.url.rstrip("/").endswith("#/agents") or "#/agents?" in page.url, (
            f"back didn't restore /agents URL: {page.url}"
        )
        # Defence: the row we drilled into is back in the list.
        page.locator(f"tr:has-text('{agent_id}')").first.wait_for(
            state="visible", timeout=10_000,
        )

        # 5. Clean console — ignore favicon races, aborted nav
        # fetches that fire normally on hash navigation, and the
        # documented by-design IC subsystem /config 404 (matches
        # test_console_loads.py's filter list).
        assert_no_console_errors_fn(
            console_messages,
            ignore_patterns=[
                r"favicon",
                r"net::ERR_ABORTED",
                r"Failed to load resource:.*status of 404",
                r"Failed to load resource: the server responded with a status of 404",
                r"DevTools failed to load source map",
            ],
        )
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            try:
                c.delete(f"/v1/agents/{agent_id}")
            except Exception:  # noqa: BLE001
                pass
            try:
                c.delete(f"/v1/llm_providers/{provider_id}")
            except Exception:  # noqa: BLE001
                pass


def test_u0021_command_palette_ctrl_k_navigates_to_workers(
    page,
    console_url: str,
) -> None:
    """U0021 — Pressing Ctrl+K opens the command palette overlay;
    typing "Workers" filters the list to the Workers page entry; the
    operator presses Enter and the URL navigates to ``#/workers``
    with the Workers page title rendered.

    The global key handler lives in ui/app.jsx:84-93 (Ctrl/Cmd+K
    toggles `paletteOpen`). The static page entry "Workers" is in
    _STATIC_PAGES (chrome.jsx:341-358). Enter on the active item
    invokes its navigate path.

    Priority — polish / keyboard shortcuts. Counted against the loop's
    ≤30% polish budget; selected because it pins a global affordance
    that's easy to break with a renderer or focus regression.
    """
    # Start on the dashboard so the palette doesn't collide with a
    # page that already shows "Workers" anywhere as a heading.
    page.goto(f"{console_url}#/", wait_until="domcontentloaded")
    page.locator("h1.page-title").first.wait_for(
        state="visible", timeout=10_000,
    )

    # Open the palette. Use Control+K (works on win/linux; macOS
    # CI normally has metaKey but Playwright maps "Control" via
    # the virtual keyboard).
    page.keyboard.press("Control+k")

    # Palette is a modal-ish overlay; the input gets focus on mount
    # (chrome.jsx:366). We type into the focused element directly to
    # be robust against future class-name churn — the palette is
    # the only thing capturing keyboard focus right after Ctrl+K.
    page.keyboard.type("Workers", delay=20)

    # Confirm we see a row containing "Workers" in the palette list
    # — wait briefly for the fuzzy matcher to filter.
    page.get_by_text("Workers", exact=False).first.wait_for(
        state="visible", timeout=5_000,
    )

    # Submit. Enter triggers the active item's navigate path.
    page.keyboard.press("Enter")

    # Verify URL and rendered page title.
    page.wait_for_url("**/console/#/workers", timeout=10_000)
    page.locator("h1.page-title").get_by_text(
        "Workers", exact=False,
    ).first.wait_for(state="visible", timeout=10_000)
