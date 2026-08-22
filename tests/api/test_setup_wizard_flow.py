"""S5 P2: the wizard's two POSTs against a stub LLM provider.

The console has no headless runtime in this lane, so the flow is driven
through the same endpoints SetupWizardSteps calls, in the same order:
_discover_models (the probe that PROVES the provider, amendment M11e) ->
POST /llm_providers -> POST /model_profiles, picking from the probe's list.
The stub replaces ``_probe_llm_models``, the single function the probe
route delegates to, so no network is touched.
"""
from __future__ import annotations

import pytest

from primer.api.routers import providers as providers_router
from primer.bootstrap.setup_state import (
    MISSING_LLM_PROVIDER,
    MISSING_MODEL_PROFILE,
)
from primer.model.model_profile import ModelProfile

DRAFT = {"provider": "ollama", "config": {"url": "http://stub:11434"}}


@pytest.fixture
def stub_probe(monkeypatch):
    """Stand in for the upstream list-models call."""
    seen: list[tuple[str, dict]] = []

    async def _fake(provider, config):
        seen.append((provider, dict(config)))
        return {
            "models": [
                {"name": "qwen3:8b", "context_length": 32000},
                {"name": "llama3.1:8b"},
            ]
        }

    monkeypatch.setattr(providers_router, "_probe_llm_models", _fake)
    return seen


async def test_profile_before_provider_is_rejected(client, app):
    """The ordering constraint the wizard's step order exists to satisfy."""
    app.state.storage_provider.get_storage(ModelProfile)._data.clear()
    r = await client.post(
        "/v1/model_profiles",
        json={
            "id": "llm-ollama--qwen3-8b",
            "description": "premature",
            "provider_id": "llm-ollama",
            "model_name": "qwen3:8b",
            "context_length": 32000,
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["extensions"]["error"] == "provider_not_found"


async def test_probe_then_provider_then_profile_completes_setup(
    client, app, stub_probe,
):
    app.state.storage_provider.get_storage(ModelProfile)._data.clear()
    before = await client.get("/v1/auth/status")
    assert before.json()["setup_complete"] is False
    assert MISSING_LLM_PROVIDER in before.json()["setup_missing"]

    # Step 1: probe the DRAFT (nothing persisted yet), then persist.
    probe = await client.post("/v1/llm_providers/_discover_models", json=DRAFT)
    assert probe.status_code == 200, probe.text
    models = probe.json()["models"]
    assert [m["name"] for m in models] == ["qwen3:8b", "llama3.1:8b"]
    assert stub_probe == [("ollama", DRAFT["config"])]

    created = await client.post(
        "/v1/llm_providers",
        json={
            "id": "llm-ollama",
            **DRAFT,
            "limits": {"max_concurrency": 4},
        },
    )
    assert created.status_code in (200, 201), created.text

    # Step 2: the SAME probe result is the pick list; no second discovery.
    picked = models[0]
    prof = await client.post(
        "/v1/model_profiles",
        json={
            "id": "llm-ollama--" + picked["name"],
            "description": "Default profile created by first-run setup.",
            "provider_id": "llm-ollama",
            "model_name": picked["name"],
            "context_length": picked["context_length"],
        },
    )
    assert prof.status_code in (200, 201), prof.text

    after = await client.get("/v1/auth/status")
    missing = after.json()["setup_missing"]
    assert MISSING_LLM_PROVIDER not in missing
    assert MISSING_MODEL_PROFILE not in missing
