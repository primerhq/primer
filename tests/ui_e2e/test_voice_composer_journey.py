"""Voice affordances appear only when speech is configured."""

from __future__ import annotations

import httpx

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._studio_helpers import open_session_in_studio

STT_ID = "ui-e2e-stt"


def _seed_stt(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # Delete-then-create: the generated PUT is update-only (it raises
        # NotFoundError when the row is absent, primer/api/routers/_crud
        # .py:417), so a leftover row from an aborted run would 409 a
        # bare POST.
        try:
            c.delete(f"/v1/stt_providers/{STT_ID}")
        except Exception:  # noqa: BLE001
            pass
        r = c.post(
            "/v1/stt_providers",
            json={
                "id": STT_ID,
                "provider": "openai",
                "default_model": "stub-asr",
                "config": {"url": "http://127.0.0.1:8791/v1"},
                "limits": {"max_concurrency": 1},
            },
        )
        assert r.status_code == 201, r.text


def _drop_stt(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        try:
            c.delete(f"/v1/stt_providers/{STT_ID}")
        except Exception:  # noqa: BLE001
            pass


def _seed_session(base_url: str, unique_suffix: str, tmp_path) -> tuple[str, str]:
    """Seed provider -> agent -> workspace template -> workspace -> session.

    The first four calls are the shape tests/ui_e2e/test_navigation_and_signals
    .py:29-72 already uses (seed_llm_provider_with, agent_model,
    /v1/workspace_providers, /v1/workspace_templates, /v1/workspaces); copy it,
    suffixing every id with `unique_suffix` and passing
    root_path=str(tmp_path). Only the session create below is new, because the
    voice affordances hang off a session composer.

    Do NOT rely on the workspace's auto-seeded "main" session: S1 P5 Task 29
    seeds it only when the system default agent is set, and default_agent_id is
    not stamped until S5 P1, which runs after S4.
    """
    agent_id = f"voice-agent-{unique_suffix}"
    provider_id = f"voice-llm-{unique_suffix}"
    wp_id = f"voice-wp-{unique_suffix}"
    tpl_id = f"voice-tpl-{unique_suffix}"

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
            "description": "voice probe",
            "model": agent_model(provider_id, "fake-model"),
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed provider failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "voice template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed template failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        wid = r.json()["id"]

    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            f"/v1/workspaces/{wid}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agent_id},
                "name": "voice",
            },
        )
        assert r.status_code == 201, r.text
        return wid, r.json()["id"]


def test_the_mic_is_absent_with_no_stt_provider(
    page, base_url, console_url, unique_suffix, tmp_path
) -> None:
    _drop_stt(base_url)
    wid, sid = _seed_session(base_url, unique_suffix, tmp_path)
    open_session_in_studio(page, console_url, wid, sid)
    page.wait_for_selector('[data-testid="chat-send-btn"]')
    assert page.query_selector('[data-testid="chat-mic-btn"]') is None


def test_the_speaker_toggle_is_absent_with_no_tts_provider(
    page, base_url, console_url, unique_suffix, tmp_path
) -> None:
    wid, sid = _seed_session(base_url, unique_suffix, tmp_path)
    open_session_in_studio(page, console_url, wid, sid)
    page.wait_for_selector('[data-testid="chat-send-btn"]')
    assert page.query_selector('[data-testid="chat-speaker-toggle"]') is None


def test_the_mic_appears_once_an_stt_provider_is_registered(
    page, base_url, console_url, unique_suffix, tmp_path
) -> None:
    _seed_stt(base_url)
    try:
        wid, sid = _seed_session(base_url, unique_suffix, tmp_path)
        open_session_in_studio(page, console_url, wid, sid)
        page.wait_for_selector('[data-testid="chat-mic-btn"]')
    finally:
        _drop_stt(base_url)
