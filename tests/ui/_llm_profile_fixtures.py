"""The sample rows and the real refusals the LLM profile tests share (Platform revamp Plan 03).

``tests/ui/test_llm_profiles_helpers.py`` (the plain-JS helpers), ``tests/ui/test_llm_profile_overlays.py`` (the two overlays, rendered) and
``tests/ui/test_llm_profiles_page.py`` (the page entry) read the same rows, so a change to the shape of a profile is made once.

* ``ROWS``: what ``GET /v1/model_profiles`` lists for the member picker: three single profiles and one aggregate (never a member candidate).
* ``FULL_AGG``: an aggregate with every field set, including the ones the aggregated overlay does not draw (``config``) and the ones the list route
  adds (``agent_count``, ``graph_node_count``) or the page annotates (``_window``): none of them may reach a PUT body.
* ``FULL_SINGLE``: the same for a single profile, whose ``config`` (``reasoning``, ``extended``) the single overlay does not draw and must send back whole.
* ``PROVIDERS``: the LLM providers the single overlay offers (``GET /v1/llm_providers`` items: the id and the provider type).
* ``router_envelopes(storage_provider)``: the bodies the REAL profile router answers for the refusals the overlays draw, so a test feeds the overlay
  what the wire carries instead of a shape somebody remembered.
"""

from __future__ import annotations

import asyncio
from typing import Any

ROWS: list[dict[str, Any]] = [
    {"id": "p-a", "kind": "single", "context_length": 32768},
    {"id": "p-b", "kind": "single", "context_length": 8192},
    {"id": "p-c", "kind": "single", "context_length": 131072},
    {"id": "agg-x", "kind": "aggregated", "members": ["p-a", "p-b"], "context_length": None},
]

FULL_AGG: dict[str, Any] = {
    "id": "agg-1", "description": "my words", "kind": "aggregated", "members": ["p-a", "p-b"],
    "strategy": "sequential", "failover_point": "mid_stream", "failover_on": "transient",
    "config": {"reasoning": "low", "extended": {"k": 1}}, "harness_id": None,
    "provider_id": None, "model_name": None, "context_length": None,
    "agent_count": 3, "graph_node_count": 1, "_window": 8192,
}

FULL_SINGLE: dict[str, Any] = {
    "id": "p-a", "description": "old", "kind": "single", "provider_id": "lp", "model_name": "qwen",
    "context_length": 32768, "config": {"reasoning": "high", "extended": {"top_k": 20}}, "harness_id": None,
    "members": None, "strategy": "sequential", "failover_point": "before_first_token",
    "failover_on": "transient_and_config", "agent_count": 2, "graph_node_count": 0,
}

PROVIDERS: list[dict[str, Any]] = [{"id": "lp", "provider": "openchat"}, {"id": "other", "provider": "anthropic"}]


def router_envelopes(storage_provider: Any) -> dict[str, dict[str, Any]]:
    """The problem bodies of three refusals, as the real ``model_profile_router`` answers them.

    ``conflict`` (409) is a create on an id that exists; ``member_not_found`` (422, field ``members``) an aggregate naming a profile that does
    not; ``provider_not_found`` (422, field ``provider_id``) a single profile on a provider that does not exist. The app is the router under ``/v1``
    with the API's own error handlers, over ``storage_provider`` (the root ``fake_storage_provider`` fixture) seeded with the single profiles
    ``p-a`` and ``p-b``.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from primer.api.deps import get_model_profile_storage
    from primer.api.errors import register_error_handlers
    from primer.api.routers.model_profiles import model_profile_router
    from primer.model.model_profile import ModelProfile

    storage = storage_provider.get_storage(ModelProfile)

    async def seed() -> None:
        for profile_id in ("p-a", "p-b"):
            await storage.create(ModelProfile(id=profile_id, description="seed", provider_id="lp", model_name="m", context_length=8192))

    asyncio.run(seed())
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(model_profile_router, prefix="/v1")
    app.state.storage_provider = storage_provider
    app.dependency_overrides[get_model_profile_storage] = lambda: storage
    client = TestClient(app, raise_server_exceptions=False)

    attempts = {
        "conflict": (409, {"id": "p-a", "description": "again", "kind": "aggregated", "members": ["p-a", "p-b"]}),
        "member_not_found": (422, {"id": "agg-new", "description": "x", "kind": "aggregated", "members": ["p-a", "p-gone"]}),
        "provider_not_found": (422, {"id": "p-new", "description": "x", "provider_id": "no-such-provider", "model_name": "m", "context_length": 5}),
    }
    out: dict[str, dict[str, Any]] = {}
    for name, (status, body) in attempts.items():
        response = client.post("/v1/model_profiles", json=body)
        assert response.status_code == status, (name, response.status_code, response.text)
        out[name] = response.json()
    return out
