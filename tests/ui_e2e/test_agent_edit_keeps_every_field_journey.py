"""An agent edit in the console keeps every field it did not change (platform revamp plan 05; master Review Focus 1).

Two fields used to be rewritten by any edit: a stored unbounded ``max_tool_turns`` (null) was saved back as 50, and a stored multi-part
``compaction_prompt`` was saved back joined into one part. This journey seeds an agent with both, and with every other field set, edits only its
description in the console, and reads the row back: every other field must be what it was. ``tests/ui/test_agent_form_body.py`` pins the body
the form sends; this pins the round trip through the real console and server.
"""

from __future__ import annotations

import time

import httpx
from playwright.sync_api import Page, expect

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-03", status="partial")

EVERY_FIELD = {
    "description": "before the journey", "temperature": 0.7, "max_tool_turns": None, "max_output_tokens": 1024,
    "tools": ["system__invoke_agent", "web__web_search"], "system_prompt": ["You triage refunds.", "Be brief."],
    "compaction_prompt": ["Keep the cited sources.", "Keep the file under edit."], "compaction_tool_access": True,
    "allow_external_tools": True, "use_workspace_instructions": False,
    "response_format": {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]},
    "tts_voice": "alloy",
}


def test_an_edit_keeps_an_unbounded_turn_cap_a_multi_part_compaction_prompt_and_every_other_field(
    page: Page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    provider_id = f"llm-agk-{unique_suffix}"
    agent_id = f"ag-agk-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": provider_id, "provider": "ollama", "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}], "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/agents", json={**EVERY_FIELD, "id": agent_id, "model": agent_model(provider_id, "fake-model")})
        assert r.status_code == 201, r.text
        before = c.get(f"/v1/agents/{agent_id}").json()
    assert before["max_tool_turns"] is None and before["compaction_prompt"] == EVERY_FIELD["compaction_prompt"], before
    try:
        open_legacy_route(page, console_url, f"agents/{agent_id}")
        expect(page.locator("#na-id")).to_have_value(agent_id, timeout=15_000)
        # The unbounded cap shows as an empty field that says so, and the two parts as one text joined with a blank line.
        expect(page.locator("#na-max-tool-turns")).to_have_value("")
        expect(page.locator("#na-max-tool-turns")).to_have_attribute("placeholder", "No limit")
        expect(page.locator("#na-compaction-prompt")).to_have_value("Keep the cited sources.\n\nKeep the file under edit.")

        page.locator("#na-description").fill("edited by the journey")
        page.get_by_test_id("agent-form-submit").click()

        deadline = time.time() + 15
        after = before
        while time.time() < deadline:
            with httpx.Client(base_url=base_url, timeout=30.0) as c:
                after = c.get(f"/v1/agents/{agent_id}").json()
            if after["description"] != "before the journey":
                break
            page.wait_for_timeout(300)
        assert after["description"] == "edited by the journey", after
        assert {k: v for k, v in after.items() if k != "description"} == {k: v for k, v in before.items() if k != "description"}
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/agents/{agent_id}")
            c.delete(f"/v1/llm_providers/{provider_id}")
