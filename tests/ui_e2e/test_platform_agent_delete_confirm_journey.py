"""Deleting a built-in setup agent from the Platform agents card says what it does (ADM-13).

The agents card used to ask the generic "Permanently delete operator?" for the ``operator`` and ``builder`` rows, and a
single confirm then made ``GET /v1/auth/status`` report ``setup_complete: false``: every admin was sent to the setup
checklist and every other user parked on a waiting screen. The unit tests (``tests/ui/test_platform_delete_confirm.py``)
run the prompt and the confirm-then-DELETE flow through MiniRacer; this journey drives the real card on the real server.

The operator is only ever CANCELLED here: confirming it would take the shared server out of setup and fail every other
journey. A throwaway agent covers the confirm path (the plain prompt, one DELETE, the card leaves the list).
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _open_agents_filtered(page, console_url: str, query: str) -> None:
    """The Platform list pages at six cards, so filter first: the card under test is then always on page one.

    The shell must be mounted before the view hash is assigned: a hash set while it is still mounting can be replaced
    by the shell's own normalisation to ``#/w/<ws>`` (the Studio view), which left one run in four looking at Studio
    (``open_view``'s docstring describes the family). Navigate once the shell is up, and once more if the page is
    still not the Platform one after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    box = page.get_by_test_id("nv-plat-filter")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", "platform:agents")
        try:
            expect(box).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            break
        except AssertionError:
            if attempt == 2:
                raise
    box.fill(query)


def test_the_operator_card_names_the_consequence_and_cancel_deletes_nothing(
    page, base_url: str, console_url: str,
) -> None:
    _open_agents_filtered(page, console_url, "operator")
    delete = page.get_by_test_id("nv-pcard-del:operator")
    expect(delete).to_be_visible(timeout=15_000)
    delete.click()

    dialog = page.locator(".modal-overlay")
    expect(dialog).to_contain_text("Delete operator", timeout=5_000)
    expect(dialog).to_contain_text("not set up")
    expect(dialog).to_contain_text("setup checklist")
    expect(dialog).to_contain_text("Re-run seed")
    expect(dialog).to_contain_text("default definition")

    dialog.get_by_role("button", name="Cancel").click()
    expect(page.locator(".modal-overlay")).to_have_count(0)
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        assert c.get("/v1/agents/operator").status_code == 200, "cancelling the prompt deleted the operator"
        assert c.get("/v1/auth/status").json().get("setup_complete") is True


def test_a_user_agent_keeps_the_plain_prompt_and_confirming_deletes_it_and_refreshes_the_list(
    page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    provider_id, agent_id = f"llm-del-{unique_suffix}", f"ag-del-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "delete-confirm journey",
            "model": agent_model(provider_id, "fake-model"),
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"
    try:
        _open_agents_filtered(page, console_url, agent_id)
        delete = page.get_by_test_id(f"nv-pcard-del:{agent_id}")
        expect(delete).to_be_visible(timeout=15_000)
        delete.click()

        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text(f"Permanently delete {agent_id}?", timeout=5_000)
        expect(dialog).not_to_contain_text("not set up")

        dialog.get_by_role("button", name="Confirm").click()
        expect(page.get_by_test_id(f"nv-pcard-del:{agent_id}")).to_have_count(0, timeout=15_000)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/agents/{agent_id}").status_code == 404
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for path in (f"/v1/agents/{agent_id}", f"/v1/llm_providers/{provider_id}"):
                try:
                    c.delete(path)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
