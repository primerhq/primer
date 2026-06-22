"""Cookbook recipe #3 regression: a meta-agent that builds agents.

A meta-agent takes a plain-language use case, discovers platform tools via the
internal semantic search (``search__search_tools`` over the ``_internal_*``
catalogue), then calls ``system__create_agent`` once to register a new agent
wired to the tool it found -- no hand-assembly.

Recipe: primerhq.github.io/docs_source/cookbook/meta-agent-builder.md

Asserts (the recipe's verified outcome):
  * the meta-agent calls ``search_tools`` and the discovery returns real hits
    (the datetime tool ``misc__get_datetime`` is in the indexed catalogue);
  * it then calls ``create_agent`` ONCE, and a NEW agent appears in
    ``GET /v1/agents`` wired to ``misc__get_datetime``;
  * the freshly built agent is immediately callable (a session bound to it
    runs to a clean terminal).

The meta-agent's behaviour is scripted (deterministic mock LLM); the
discovery + creation platform paths are real. Internal semantic search is
gated on an embedder + pgvector, so the subsystem is bootstrapped first
(POST /bootstrap is async; we poll until it succeeds).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = pytest.mark.asyncio


_SSP = {
    "provider": "pgvector",
    "config": {
        "hostname": "localhost", "port": 5432, "database": "primer_e2e",
        "username": "primer", "password": "primer", "db_schema": "public",
    },
}
_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_EMBED = {
    "provider": "huggingface",
    "models": [{"name": _EMBED_MODEL, "dim": 384}],
    "config": {"token": "hf-placeholder"},
    "limits": {"max_concurrency": 1},
}


async def _wait_bootstrap(client: httpx.AsyncClient, *, timeout_s: float = 180.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = "unknown"
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(
            "/v1/internal_collections/bootstrap/status",
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        assert r.status_code == 200, r.text
        last = r.json().get("status")
        if last == "succeeded":
            return
        if last == "failed":
            pytest.skip(f"internal-collections bootstrap failed: {r.json().get('error')!r}")
        await asyncio.sleep(0.5)
    pytest.skip(f"bootstrap did not complete in {timeout_s}s (last={last!r})")


@smk("SMK-COOKBOOK-03")
@requires("embedder", "pgvector")
async def test_meta_agent_builds_from_use_case(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    registry, base_url = mock_llm
    sfx = unique_suffix
    ssp_id = f"ssp-meta-{sfx}"
    emb_id = f"emb-meta-{sfx}"
    built_id = f"datetime-agent-{sfx}"

    config_active = False
    try:
        # --- Activate internal semantic search so search_tools is real ---
        r = await authed_client.post("/v1/ssp", json={"id": ssp_id, **_SSP})
        assert r.status_code in (201, 409), r.text
        r = await authed_client.post(
            "/v1/embedding_providers", json={"id": emb_id, **_EMBED},
        )
        assert r.status_code in (201, 409), r.text
        put = await authed_client.put(
            "/v1/internal_collections/config",
            json={
                "embedding_provider_id": emb_id,
                "embedding_model": _EMBED_MODEL,
                "search_provider_id": ssp_id,
            },
        )
        assert put.status_code == 200, put.text
        config_active = True
        boot = await authed_client.post(
            "/v1/internal_collections/bootstrap",
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        assert boot.status_code == 202, boot.text
        await _wait_bootstrap(authed_client)

        # --- The scripted meta-agent ------------------------------------
        # Rule 1: discover tools. Rule 2 (after the search hits): create the
        # datetime agent wired to the tool it "found". Rule 3: report done.
        meta = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"meta{sfx}",
            scenario=f"scripted:meta-{sfx}",
            tools=["search__search_tools", "system__create_agent"],
            system_prompt=["You build agents from a use case by discovering tools."],
            rules=[
                Rule(when_tool_result=False,
                     emit_tool="search__search_tools",
                     emit_args={"query": "return the current date and time",
                                "top_k": 5}),
                Rule(when_last_tool_result_contains="hits",
                     emit_tool="system__create_agent",
                     emit_args={"entity": {
                         "id": built_id,
                         "description": "Returns the current date and time on request.",
                         "model": {"provider_id": f"p-meta{sfx}",
                                   "model_name": f"scripted:meta-{sfx}"},
                         "tools": ["misc__get_datetime"],
                         "system_prompt": ["Return the current date and time."],
                     }}),
                Rule(when_tool_result=True, emit_text="created datetime-agent"),
            ],
        )

        wid = await make_local_workspace(authed_client, suffix=f"meta{sfx}", root=tmp_path)
        sid = await start_agent_session(
            authed_client, workspace_id=wid, agent_id=meta["agent_id"],
            instructions="Use case: build an agent that returns the current date and time.",
        )
        final = await wait_terminal(authed_client, sid, timeout_s=60)
        assert final.get("status") == "ended", final

        # search_tools was dispatched AND returned real hits including the
        # datetime tool (proves the discovery step ran against the catalogue).
        search_reqs = [
            req for req in registry.requests
            if req.get("model") == f"scripted:meta-{sfx}"
        ]
        assert search_reqs, "meta-agent made no LLM calls"
        # The second LLM turn must have seen a tool result containing hits.
        saw_hits = any(
            "misc__get_datetime" in str(m.get("content", ""))
            for req in search_reqs
            for m in req.get("messages", [])
            if m.get("role") == "tool"
        )
        assert saw_hits, (
            "search_tools did not surface misc__get_datetime in the catalogue; "
            "the meta-agent could not have discovered it"
        )

        # The new agent appears, wired to the discovered tool.
        got = await authed_client.get(f"/v1/agents/{built_id}")
        assert got.status_code == 200, f"created agent missing: {got.text}"
        assert got.json()["tools"] == ["misc__get_datetime"], got.json()

        # The freshly built agent is immediately callable.
        run = await start_agent_session(
            authed_client, workspace_id=wid, agent_id=built_id,
            instructions="what time is it",
        )
        run_final = await wait_terminal(authed_client, run, timeout_s=60)
        assert run_final.get("status") == "ended", run_final
    finally:
        await authed_client.delete(f"/v1/agents/{built_id}")
        if config_active:
            await authed_client.delete("/v1/internal_collections/config")
        await authed_client.delete(f"/v1/embedding_providers/{emb_id}")
        await authed_client.delete(f"/v1/ssp/{ssp_id}")
