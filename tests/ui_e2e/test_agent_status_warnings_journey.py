"""The agent page shows the status endpoint's output-cap warning (01a10c6b item 2).

``GET /v1/agents/{id}/status`` returns a ``warnings`` list next to ``issues``; the status panel on the agent's page lists
the warnings under the verdict (``data-testid="ag-status-warnings"``). The static tests in
``tests/ui/test_agents_status_warnings.py`` only read the source, so a panel that never rendered its warnings would pass
them (the lead's survivor V5): this journey drives the real page.

Two agents on one provider whose only model has a 4096-token window: one with ``max_output_tokens`` equal to the window
(the panel must warn, and the verdict stays "All references resolve": a warning is not an issue) and one with a sane cap
(the control: no warnings block, and only after the panel has resolved, so the absence is not just "not loaded yet").
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from playwright.sync_api import expect

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-03", status="partial")

WINDOW = 4096


@contextmanager
def _provider_and_agents(base_url: str, suffix: str) -> Iterator[tuple[str, str]]:
    """A provider with a ``WINDOW``-token model and two agents on it; yields ``(capped_agent_id, sane_agent_id)``."""
    provider_id = f"llm-cap-{suffix}"
    capped, sane = f"ag-cap-{suffix}", f"ag-sane-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": WINDOW}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"
        for agent_id, cap in ((capped, WINDOW), (sane, 512)):
            r = c.post("/v1/agents", json={
                "id": agent_id,
                "description": "output cap status journey",
                "model": agent_model(provider_id, "fake-model"),
                "tools": [],
                "system_prompt": ["test"],
                "max_output_tokens": cap,
            })
            assert r.status_code == 201, f"seed agent {agent_id} failed: {r.text}"
    try:
        yield capped, sane
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for path in (f"/v1/agents/{capped}", f"/v1/agents/{sane}", f"/v1/llm_providers/{provider_id}"):
                try:
                    c.delete(path)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass


def test_the_agent_page_lists_the_output_cap_warning_and_keeps_the_verdict_green(
    page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    with _provider_and_agents(base_url, unique_suffix) as (capped, _sane):
        open_legacy_route(page, console_url, f"agents/{capped}")
        page.locator("h1.page-title").get_by_text(capped).first.wait_for(state="visible", timeout=10_000)

        warnings = page.get_by_test_id("ag-status-warnings")
        expect(warnings).to_be_visible(timeout=15_000)
        expect(warnings).to_contain_text("max_output_tokens", timeout=5_000)
        expect(warnings).to_contain_text(f"max_output_tokens ({WINDOW})")
        # A warning is not an issue: the headline still says every reference resolves.
        expect(page.get_by_text("All references resolve").first).to_be_visible(timeout=5_000)


def test_the_agent_page_shows_no_warnings_block_for_a_cap_that_fits(
    page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    with _provider_and_agents(base_url, unique_suffix) as (_capped, sane):
        open_legacy_route(page, console_url, f"agents/{sane}")
        page.locator("h1.page-title").get_by_text(sane).first.wait_for(state="visible", timeout=10_000)

        # The control must not pass because the panel has not loaded: wait for its verdict first.
        expect(page.get_by_text("All references resolve").first).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("ag-status-warnings")).to_have_count(0)
