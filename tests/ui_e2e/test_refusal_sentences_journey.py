"""A refused delete and a refused duplicate create say what happened in words (ADM-12 and ADM-20 of the 2026-10-08 admin review).

ADM-12: deleting a model profile that an agent uses toasted ``Delete refused: in_use_by: 1 agent(s) reference '<profile>' (first: '<agent>')``.
ADM-20: creating a channel on a provider and external id that another channel already holds toasted
``Channel with provider_id='<p>', external_id='<e>' already exists (id='<channel>')``.
Both are sentences the server builds for a machine to match; ``window.primerApi.readRefusal`` (``ui/foundation/api.js``) says them plainly. The unit tests
(``tests/ui/test_refusal_reader.py``) run the reader on the REAL envelopes in MiniRacer; these journeys drive the real Platform page and the real new-channel
dialog on the real server and read the toast.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests._support.model_profiles import agent_model, profile_id_for, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")

_FAKE_DISCORD_TOKEN = "x" * 60


def _open_filtered(page, console_url: str, nav: str, query: str) -> None:
    """The Platform list pages at six cards, so filter first. The shell must be mounted before the view hash is assigned (a hash set while it mounts can be
    replaced by the shell's own normalisation to the Studio view), so wait for it and navigate once more if the page is not the Platform one."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    box = page.get_by_test_id("nv-plat-filter")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", f"platform:{nav}")
        try:
            expect(box).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            break
        except AssertionError:
            if attempt == 2:
                raise
    box.fill(query)


def test_deleting_a_model_profile_an_agent_uses_is_refused_in_words_and_deletes_nothing(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"refusal-{unique_suffix}"
    model = "refusal-model"
    profile_id = profile_id_for(provider_id, model)
    agent_id = f"refusal-agent-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=60.0) as c:
        r = seed_llm_provider_with(c, {
            "id": provider_id, "provider": "ollama", "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": model, "context_length": 4096}], "limits": {"max_concurrency": 1},
        })
        assert r.status_code in (200, 201), r.text
        r = c.post("/v1/agents", json={
            "id": agent_id, "description": "refusal journey", "model": agent_model(provider_id, model), "tools": [], "system_prompt": ["x"],
        })
        assert r.status_code == 201, r.text
    try:
        _open_filtered(page, console_url, "profiles", profile_id)
        delete = page.get_by_test_id(f"nv-pcard-del:{profile_id}")
        expect(delete).to_be_visible(timeout=15_000)
        delete.click()
        page.locator(".modal-overlay").get_by_role("button", name="Confirm").click()

        toast = page.get_by_text("is still in use by an agent", exact=False).first
        expect(toast).to_be_visible(timeout=10_000)
        expect(toast).to_contain_text(f"Delete refused: '{profile_id}' is still in use by an agent, for example {agent_id}. Remove or change that first.")
        expect(toast).not_to_contain_text("in_use_by")
        expect(toast).not_to_contain_text("agent(s)")
        # The refusal protected the row: it is still there, and so is the card.
        expect(page.get_by_test_id(f"nv-pcard-del:{profile_id}")).to_be_visible()
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/model_profiles/{profile_id}").status_code == 200
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for path in (f"/v1/agents/{agent_id}", f"/v1/model_profiles/{profile_id}", f"/v1/llm_providers/{provider_id}"):
                try:
                    c.delete(path)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass


def test_creating_a_channel_on_a_taken_room_says_which_channel_holds_it(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"cp-refusal-{unique_suffix}"
    first_id = f"ch-refusal-a-{unique_suffix}"
    second_id = f"ch-refusal-b-{unique_suffix}"
    external_id = "123456789012345678"
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post("/v1/channel_providers", json={"id": provider_id, "provider": "discord", "config": {"bot_token": _FAKE_DISCORD_TOKEN}})
            assert r.status_code == 201, r.text
            r = c.post("/v1/channels", json={"id": first_id, "provider": "discord", "provider_id": provider_id, "external_id": external_id})
            assert r.status_code == 201, r.text
        _open_filtered(page, console_url, "channels", first_id)

        page.get_by_test_id("nv-plat-create").click()
        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text("New channel", timeout=10_000)
        dialog.locator("select").first.select_option(provider_id)
        dialog.get_by_placeholder("auto-generated").fill(second_id)
        dialog.get_by_placeholder("C0123ABC456 / chat-id / snowflake").fill(external_id)
        dialog.get_by_role("button", name="Create").click()

        toast = page.get_by_text("already exists", exact=False).first
        expect(toast).to_be_visible(timeout=10_000)
        expect(toast).to_contain_text(f"A channel with provider_id {provider_id} and external_id {external_id} already exists: {first_id}.")
        expect(toast).not_to_contain_text("provider_id='")
        # The form keeps what was typed, and nothing was created.
        expect(dialog).to_be_visible()
        expect(dialog.get_by_placeholder("auto-generated")).to_have_value(second_id)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/channels/{second_id}").status_code == 404
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for path in (f"/v1/channels/{second_id}", f"/v1/channels/{first_id}", f"/v1/channel_providers/{provider_id}"):
                try:
                    c.delete(path)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
