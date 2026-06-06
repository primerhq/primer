"""Internal Collections subsystem-inactive page + Graph create modal.

Covers:
* U0040 — Internal Collections page shows IC-OFF state and Configure
  CTA loads a form modal cleanly.
* U0028 — Graph create modal navigates to graph detail and renders
  the status panel.
"""

from __future__ import annotations

import httpx


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-04", "SMK-UI-05", status="partial")


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# U0040 — Internal Collections page IC-OFF state + Configure CTA
# ---------------------------------------------------------------------------


def test_u0040_internal_collections_page_shows_off_state_and_configure_cta(
    page,
    base_url: str,
    console_url: str,
    console_messages,
    assert_no_console_errors_fn,
) -> None:
    """U0040 — With the IC subsystem inactive (no config row), opening
    /subsystems/internal-collections renders the OFF state card with
    the documented "Internal Collections is not configured" copy
    and a "Configure" CTA. Clicking Configure opens a ConfigureModal
    without console errors.

    Priority 3 — anomaly surface. The OFF-state card is at
    internal-collections.jsx:71-105 (InactiveCard). Defends against
    a regression where the OFF state collapses to a blank page or
    the Configure CTA loses its onClick handler.

    Precondition: drain any existing IC config row via API so we land
    on the OFF state.
    """
    # Drain any existing IC config row.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        try:
            c.delete("/v1/internal_collections/config")
        except Exception:  # noqa: BLE001
            pass

    page.goto(
        f"{console_url}#/subsystems/internal-collections",
        wait_until="domcontentloaded",
    )
    page.locator("h1.page-title").first.wait_for(
        state="visible", timeout=10_000,
    )

    # Documented OFF-state head copy.
    page.get_by_text(
        "Internal Collections is not configured", exact=False,
    ).first.wait_for(state="visible", timeout=10_000)

    # Configure CTA must be visible + clickable.
    configure_btn = page.get_by_role("button", name="Configure").first
    configure_btn.wait_for(state="visible", timeout=5_000)
    configure_btn.click()

    # The ConfigureModal opens — assert a .modal is now in the DOM.
    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=5_000)

    # Defence: no console errors during the OFF state render +
    # Configure click. Ignore the by-design IC 404 (the page itself
    # polls /internal_collections/config which 404s on the OFF
    # state — that's the documented signal, not an error).
    assert_no_console_errors_fn(
        console_messages,
        ignore_patterns=[
            r"favicon",
            r"DevTools failed to load source map",
            r"Failed to load resource:.*status of 404",
        ],
    )


# ---------------------------------------------------------------------------
# U0028 — Graph create modal navigates + status panel renders
# ---------------------------------------------------------------------------


def test_u0028_graph_create_modal_navigates_and_renders_status(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0028 — Seed an agent via API, open /graphs, click "New graph",
    submit the modal with the auto-seeded agent. Assert:

    * modal closes,
    * URL navigates to ``#/graphs/<new-id>``,
    * the GraphStatusPanel renders one of its documented states
      ("All references resolve" / "N issues found" / "Checking
      references…").

    Priority 1 — mutation feedback for the Graph create flow. The
    NewGraphModal seeds a minimal agent→terminal skeleton
    (graphs.jsx:184-201) with one static edge from start → end,
    entry_node_id="start". GraphStatusPanel polls
    /v1/graphs/{id}/status every 30s and surfaces ok=true/false
    in three documented states.
    """
    provider_id = f"llm-u0028-{unique_suffix}"
    agent_id = f"ag-u0028-{unique_suffix}"
    graph_id = f"graph-u0028-{unique_suffix}"
    # Seed LLM provider + agent so the modal's "seed agent" dropdown
    # has a deterministic option.
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
            "description": "u0028 graph create probe",
            "model": {
                "provider_id": provider_id,
                "model_name": "fake-model",
            },
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"

    try:
        page.goto(f"{console_url}#/graphs", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Graphs", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Open the modal.
        page.get_by_role("button", name="New graph").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # Fill ID input so we can predict + clean up. The first input
        # in the modal is the ID field (per NewGraphModal field order).
        id_input = modal.locator("input.input").first
        id_input.fill(graph_id)

        # Select the seeded agent in the agent dropdown (the modal
        # auto-selects the first agent on mount, but we set explicitly
        # so the test doesn't depend on list ordering).
        modal.locator("select.select").first.select_option(value=agent_id)

        # Submit.
        modal.get_by_role("button", name="Create").first.click()

        # URL must navigate to /graphs/<id>.
        page.wait_for_url(
            lambda url: f"#/graphs/{graph_id}" in url,
            timeout=15_000,
        )
        page.locator("h1.page-title").get_by_text(
            graph_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # GraphStatusPanel renders one of its three documented states
        # (graphs.jsx:367-369): "Checking references…" while loading,
        # "All references resolve" if ok, "N issues found" if not.
        # Use a regex-friendly substring search so any of the three
        # satisfies the contract.
        status_phrases = [
            "All references resolve",
            "issues found",
            "Checking references",
        ]
        deadline = page.evaluate("performance.now()") + 30_000
        rendered = False
        while page.evaluate("performance.now()") < deadline:
            body_text = page.locator("body").text_content() or ""
            if any(p in body_text for p in status_phrases):
                rendered = True
                break
            page.wait_for_timeout(500)
        assert rendered, (
            f"GraphStatusPanel never rendered one of {status_phrases!r} "
            f"within 30s after graph create"
        )
    finally:
        _cleanup(base_url, [
            f"/v1/graphs/{graph_id}",
            f"/v1/agents/{agent_id}",
            f"/v1/llm_providers/{provider_id}",
        ])
