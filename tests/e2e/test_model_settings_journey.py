"""E2E journey: a profile's per-provider settings, from save to wire (PMS Task 8).

An openchat provider of flavor vllm is seeded against the mock LLM (it records every request
body it receives), so the journey asserts on what the model actually got:

* the provider's settings route (Task 5) names the wire (openchat/vllm) and lists its settings,
  ``top_k`` among them;
* a profile whose settings the wire accepts (the ``high`` level plus ``top_k`` and a sibling
  key inside ``chat_template_kwargs``) saves (201), and a turn under it reaches the model with
  ``top_k: 20`` and ``chat_template_kwargs == {"enable_thinking": true, "foo": 1}``: the level
  sets only its own leaf and the sibling key stays (spec section 4's merge);
* a profile whose extended holds a key the wire does not name (``topk``) is refused on save
  (422), the message naming the key and the wire's accepted keys (spec section 6);
* a profile with a custom reasoning value (``{"custom": "medium"}``) saves (201), and a turn
  under it reaches the model with ``reasoning_effort: "medium"`` written verbatim to the
  wire's custom target, and no ``chat_template_kwargs`` from the level.

Written now, run only after the other PMS tasks are merged (the e2e suite is CPU-exclusive,
AGENTS.md section 5): ``PRIMER_RUN_E2E=1 PRIMER_E2E_PORT=8765``, ``-n0``.
"""

from __future__ import annotations

import pytest

from tests._support.mock_llm import Rule
from tests._support.model_profiles import agent_model, profile_body, seed_llm_provider, seed_profile
from tests._support.runs import make_local_workspace, start_agent_session, wait_completed

pytestmark = pytest.mark.asyncio


async def test_the_model_settings_of_a_profile_reach_the_wire(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    suffix = unique_suffix
    pid = f"msj-{suffix}"
    hi, bad, cust = f"msj-hi-{suffix}", f"msj-bad-{suffix}", f"msj-cust-{suffix}"

    # The provider: openchat, flavor vllm, at the mock. No declared models: the profiles are
    # seeded below, explicitly, with the settings this journey is about.
    r = await seed_llm_provider(
        authed_client,
        {
            "id": pid, "provider": "openchat",
            "models": [],
            "config": {"url": base_url, "flavor": "vllm"},
            "limits": {"max_concurrency": 4},
        },
    )
    assert r.status_code in (200, 201), r.text

    # The provider's settings for its wire (Task 5's route): the wire is openchat/vllm and its
    # settings include top_k, the key the profile's settings below are checked against.
    r = await authed_client.get(f"/v1/llm_providers/{pid}/model_settings")
    assert r.status_code == 200, r.text
    cat = r.json()
    assert cat["provider_type"] == "openchat" and cat["flavor"] == "vllm"
    assert cat["wire"] == "openchat/vllm"
    assert "top_k" in {s["key"] for s in cat["settings"]}

    # A profile whose settings the wire accepts: the level high plus the wire's own keys.
    await seed_profile(
        authed_client, pid, hi,
        config={"reasoning": "high", "extended": {"top_k": 20, "chat_template_kwargs": {"foo": 1}}},
    )

    # A key the wire does not name is refused on save, the message naming the key and the wire.
    r = await authed_client.post(
        "/v1/model_profiles", json=profile_body(pid, bad, config={"extended": {"topk": 1}}),
    )
    assert r.status_code == 422, r.text
    ext = r.json()["extensions"]
    assert ext["error"] == "invalid_model_setting"
    assert ext["field"] == "config.extended.topk"
    assert ext["message"].startswith("extended.topk is not a setting for openchat/vllm; accepted: ")

    # A custom reasoning value: written verbatim to the wire's custom target, no mapping.
    await seed_profile(authed_client, pid, cust, config={"reasoning": {"custom": "medium"}})

    wid = await make_local_workspace(authed_client, suffix=suffix, root=tmp_path)

    async def _turn(model: str, aid: str) -> dict:
        """One scripted turn under the agent's profile; the request body the mock recorded
        (what the model actually got)."""
        ar = await authed_client.post("/v1/agents", json={
            "id": aid, "description": "msj settings agent",
            "model": agent_model(pid, model), "tools": [],
            "system_prompt": ["You are a scripted test agent."],
        })
        assert ar.status_code in (200, 201), ar.text
        registry.register(model, [Rule(emit_text="ok")])
        before = len(registry.requests)
        sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=aid)
        row = await wait_completed(authed_client, sid)
        assert (row.get("turn_no") or 0) >= 1, row
        reqs = [q for q in registry.requests[before:] if q.get("model") == model]
        assert reqs, "the turn called the scripted provider"
        return reqs[0]

    # One turn under each saved profile.
    hi_body = await _turn(hi, f"msj-a-hi-{suffix}")
    assert hi_body.get("top_k") == 20, hi_body
    # The level sets only its own leaf (enable_thinking) and the sibling key stays.
    assert hi_body.get("chat_template_kwargs") == {"enable_thinking": True, "foo": 1}, hi_body

    cust_body = await _turn(cust, f"msj-a-cust-{suffix}")
    # The custom value is verbatim on the wire's custom target; the level set no template switch.
    assert cust_body.get("reasoning_effort") == "medium", cust_body
    assert "chat_template_kwargs" not in cust_body, cust_body
