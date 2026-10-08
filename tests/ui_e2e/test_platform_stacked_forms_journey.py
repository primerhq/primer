"""Platform > Agents, Graphs and Approval policies: "New ..." opens the dialog on the page, not on top of the legacy list (ADM-06 of the 2026-10-08 admin review).

The press used to open the LEGACY list overlay with ``section = "new"`` so its own effect opened the dialog on top of it: a modal with the full legacy table (and its own
"New ..." button) behind it, two surfaces deep for one action. These journeys drive the real Platform pages: no management overlay is mounted while the dialog is open,
Cancel returns to the card grid with the address bar untouched, and a created agent or graph lands on its own detail overlay with its card in the grid behind it.
Approval policies are opened and cancelled only: creating one needs a tool and a rule, and its dialog reports a created policy through ``onClose`` with no row.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from playwright.sync_api import expect

from tests._support.model_profiles import agent_model, profile_id_for, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _open_platform_page(page, console_url: str, nav: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own
    normalisation to the Studio view), so wait for it and navigate once more if the page is not the Platform one after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id(f"nv-plat-page:{nav}")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", f"platform:{nav}")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


@contextmanager
def _seeded_llm_provider(base_url: str, suffix: str) -> Iterator[str]:
    """A placeholder ollama LLM provider (and the model profile it brings) so the agent form has a profile to pick; deleted on exit."""
    pid = f"llm-adm06-{suffix}"
    body = {
        "id": pid,
        "provider": "ollama",
        "config": {"url": "http://127.0.0.1:9999"},
        "models": [{"name": "fake-model", "context_length": 4096}],
        "limits": {"max_concurrency": 1},
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        assert seed_llm_provider_with(c, body).status_code == 201
    try:
        yield pid
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            try:
                c.delete(f"/v1/llm_providers/{pid}")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def _delete(base_url: str, path: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        try:
            c.delete(path)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def _plain_url(page) -> str:
    return page.url.replace("%3A", ":")


def test_new_agent_opens_the_form_with_no_list_behind_it_and_a_created_agent_lands_on_its_detail(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    agent_id = f"ag-adm06-{unique_suffix}"
    with _seeded_llm_provider(base_url, unique_suffix) as provider_id:
        try:
            _open_platform_page(page, console_url, "agents")

            page.get_by_test_id("nv-plat-create").click()

            modal = page.locator(".modal").first
            modal.wait_for(state="visible", timeout=10_000)
            expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
            assert "overlay=" not in page.url, page.url

            modal.locator("#na-id").fill(agent_id)
            modal.locator("#na-description").fill("adm06 journey")
            modal.get_by_test_id(f"agent-profile-row-{profile_id_for(provider_id, 'fake-model')}").click()
            modal.get_by_role("button", name="Create").click()

            # The created agent lands on its own detail overlay, and its card is in the grid behind it.
            expect(page.get_by_test_id("nv-overlay-body")).to_be_visible(timeout=15_000)
            assert f"overlay=agents::{agent_id}" in _plain_url(page), page.url
            page.get_by_test_id("nv-plat-filter").fill(agent_id)
            expect(page.get_by_test_id(f"nv-pcard-del:{agent_id}")).to_be_attached(timeout=15_000)
        finally:
            _delete(base_url, f"/v1/agents/{agent_id}")


def test_cancelling_the_new_agent_form_leaves_the_card_grid_and_the_url_alone(page, console_url: str) -> None:
    _open_platform_page(page, console_url, "agents")
    before = page.url

    page.get_by_test_id("nv-plat-create").click()
    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=10_000)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    modal.get_by_role("button", name="Cancel").click()

    expect(page.locator(".modal")).to_have_count(0)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert page.url == before, (before, page.url)


def test_new_graph_opens_the_dialog_with_no_list_behind_it_and_a_created_graph_lands_on_its_detail(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    agent_id = f"ag-adm06g-{unique_suffix}"
    graph_id = f"graph-adm06-{unique_suffix}"
    with _seeded_llm_provider(base_url, unique_suffix) as provider_id:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post(
                "/v1/agents",
                json={
                    "id": agent_id, "description": "adm06 graph seed", "model": agent_model(provider_id, "fake-model"),
                    "tools": [], "system_prompt": ["test"],
                },
            )
            assert r.status_code == 201, r.text
        try:
            _open_platform_page(page, console_url, "graphs")

            page.get_by_test_id("nv-plat-create").click()

            modal = page.locator(".modal").first
            modal.wait_for(state="visible", timeout=10_000)
            expect(modal).to_contain_text("New graph")
            expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
            assert "overlay=" not in page.url, page.url

            modal.locator("input.input").first.fill(graph_id)
            modal.locator("select.select").first.select_option(value=agent_id)
            modal.get_by_role("button", name="Create").first.click()

            expect(page.get_by_test_id("nv-overlay-body")).to_be_visible(timeout=15_000)
            assert f"overlay=graphs::{graph_id}" in _plain_url(page), page.url
            page.get_by_test_id("nv-plat-filter").fill(graph_id)
            expect(page.get_by_test_id(f"nv-pcard-del:{graph_id}")).to_be_attached(timeout=15_000)
        finally:
            _delete(base_url, f"/v1/graphs/{graph_id}")
            _delete(base_url, f"/v1/agents/{agent_id}")


def test_new_approval_policy_opens_the_dialog_with_no_list_behind_it_and_cancel_leaves_the_grid_alone(page, console_url: str) -> None:
    _open_platform_page(page, console_url, "approvals")
    before = page.url

    page.get_by_test_id("nv-plat-create").click()

    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=10_000)
    expect(modal).to_contain_text("New approval policy")
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert "overlay=" not in page.url, page.url

    modal.get_by_role("button", name="Cancel").click()
    expect(page.locator(".modal")).to_have_count(0)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert page.url == before, (before, page.url)
