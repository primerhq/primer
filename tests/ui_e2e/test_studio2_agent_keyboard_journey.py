"""Keyboard-only agent creation in Studio2: palette -> form -> save.

Zero mouse events: this is the enforcement test for the spec's
'no mouse-only affordance' rule (spec section 7). Sync test + sync
httpx cleanup, matching the suite convention (see test_agents_create).
"""

from __future__ import annotations

import uuid

import httpx


def test_create_agent_keyboard_only(page, console_url, base_url) -> None:
    suffix = uuid.uuid4().hex[:8]
    agent_id = f"s2-kbd-{suffix}"
    provider_id = f"llm-s2-{suffix}"
    profile_id = f"prof-s2-{suffix}"

    # Precondition: a provider + a model profile to bind (post-cutover,
    # agents reference a profile, not a (provider, model) pair).
    with httpx.Client(base_url=base_url, timeout=30.0) as api:
        resp = api.post("/v1/llm_providers", json={
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "limits": {"max_concurrency": 1},
        })
        assert resp.status_code == 201, resp.text
        resp = api.post("/v1/model_profiles", json={
            "id": profile_id,
            "description": "keyboard e2e profile",
            "provider_id": provider_id,
            "model_name": "fake-model",
            "context_length": 4096,
        })
        assert resp.status_code == 201, resp.text

    page.goto(console_url + "#/studio2")
    page.wait_for_selector('[data-testid="s2-root"]')

    # Palette -> New agent (no mouse from here on).
    page.keyboard.press("Control+k")
    page.wait_for_selector('[data-testid="s2-palette-input"]')
    page.fill('[data-testid="s2-palette-input"]', "New agent")
    page.keyboard.press("Enter")
    page.wait_for_selector('[data-testid="s2-agent-doc"]')

    # Fill the form; every field is keyboard-focusable.
    page.focus('[data-testid="s2-agent-id"]')
    page.keyboard.type(agent_id)
    page.focus('[data-testid="s2-agent-description"]')
    page.keyboard.type("keyboard-only e2e agent")
    # Options inside a closed <select> are never "visible"; wait attached.
    page.wait_for_selector(
        f'[data-testid="s2-agent-profile"] >> option[value="{profile_id}"]',
        state="attached",
    )
    page.select_option('[data-testid="s2-agent-profile"]', profile_id)

    # Ctrl+S saves; the document reopens under the real id.
    page.keyboard.press("Control+s")
    page.wait_for_selector(f"text=Agent created: {agent_id}")

    # The navigator shows the new agent (g a -> filter). Esc first so
    # the chord is not swallowed by the focused form input.
    page.keyboard.press("Escape")
    page.keyboard.press("g")
    page.keyboard.press("a")
    page.wait_for_selector('[data-testid="s2-nav-filter"]')
    page.fill('[data-testid="s2-nav-filter"]', agent_id)
    page.wait_for_selector(f'[data-testid="s2-nav"] >> text={agent_id}')

    # Cleanup via the API (sync client, suite convention).
    with httpx.Client(base_url=base_url, timeout=30.0) as api:
        resp = api.delete(f"/v1/agents/{agent_id}")
        assert resp.status_code in (200, 204), resp.text
        api.delete(f"/v1/model_profiles/{profile_id}")
        api.delete(f"/v1/llm_providers/{provider_id}")
