"""Cookbook recipe #14 regression: Describe-to-Deploy App Builder.

An ``app-builder`` agent provisions a whole mini-app from one request, using
the INTERNAL CRUD toolsets (the surface BEYOND ``create_agent`` that the
meta-agent recipe already covers): it creates a collection, seeds a document,
creates a summarizer agent, creates a digest graph, and creates a scheduled
trigger plus a ``graph_fresh_session`` subscription. The recipe then fires the
trigger once to prove the assembled app actually RUNS.

Recipe: primerhq.github.io/docs_source/cookbook/app-builder.md

This closes the internal CRUD coverage gap beyond ``system__create_agent``:

  * ``system__create_collection`` + ``system__put_document`` -- a real,
    searchable knowledge base built through agent tools;
  * ``system__create_agent`` -- the summarizer (already proven by recipe #3,
    re-exercised here as one leg of an assembly);
  * ``system__create_graph`` -- a runnable begin -> agent -> end graph;
  * ``trigger__create`` + ``trigger__create_subscription`` -- a scheduled
    trigger with a ``graph_fresh_session`` subscription pointing at the graph
    (all three trigger tools are callable agent tools registered by the
    always-on ``trigger`` toolset);
  * ``trigger__fire_now`` -- fire the assembled app once.

The builder agent's tool sequence is SCRIPTED with the deterministic mock LLM
(one CRUD call per turn, sequenced on the previous tool result); the CRUD
platform paths -- validation, persistence, indexing, dispatch -- are REAL. The
test proves every entity the agent created PERSISTED (via REST GET), the seeded
document is SEARCHABLE, and the fired graph session ran to terminal
``completed`` with an on-disk transcript (the assembled app is runnable, not
just defined).

CRUD-tool sequencing trap: rule matching is FIRST-match-wins and every
``create_*`` result echoes the whole entity (a ``put_document`` result still
carries the ``collection_id``), so rules are ordered LATEST-FIRST and keyed on
a token UNIQUE to the immediately-preceding step's result (``"embedder"`` ->
``"document-`` -> ``"system_prompt"`` -> ``"nodes"``). Otherwise an earlier
rule re-matches a later result and the agent loops forever on one tool.

Internal semantic search is gated on an embedder + pgvector, so the test is
``@requires("embedder", "pgvector")``.

Run with:
    PRIMER_RUN_E2E=1 uv run pytest tests/e2e/test_cookbook_app_builder.py -n0 -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_local_workspace, make_scripted_agent, wait_terminal
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = [pytest.mark.asyncio]


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

_DOC_PATH = "news/today.md"
_DOC_BODY = (
    "Today's headlines: the platform shipped generic collections; "
    "the channel media pipeline went live; the release pipeline is staged."
)


def _dispatched_session_id(fire_result: dict) -> str | None:
    for res in fire_result.get("results", []):
        if res.get("ok") and res.get("artefact_id"):
            return res["artefact_id"]
    return None


def _builder_tool_calls(transcript: str) -> list[str]:
    """Return the ordered list of tool names the builder agent called."""
    names: list[str] = []
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # On-disk transcript records tool_call as a top-level event
        # ({"kind": "tool_call", "payload": {"name": ...}}) and/or as an
        # assistant message part; cover both shapes.
        if obj.get("kind") == "tool_call":
            payload = obj.get("payload") or {}
            name = payload.get("name") or payload.get("tool_name")
            if name:
                names.append(name)
            continue
        if obj.get("role") == "assistant":
            for part in obj.get("parts", []):
                if part.get("type") == "tool_call":
                    name = part.get("tool_name") or part.get("name")
                    if name:
                        names.append(name)
    return names


@smk("SMK-COOKBOOK-14")
@requires("embedder", "pgvector")
async def test_app_builder_provisions_and_runs_mini_app(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    registry, base_url = mock_llm
    sfx = unique_suffix
    scenario = f"scripted:builder-{sfx}"

    coll_id = f"digest-kb-{sfx}"
    summarizer_id = f"summarizer-{sfx}"
    graph_id = f"digest-graph-{sfx}"
    trigger_slug = f"nightly-digest-{sfx}"
    ssp_id = f"ssp-{sfx}"
    emb_id = f"emb-{sfx}"
    # The summarizer agent the builder creates runs inside the fired graph; it
    # gets its OWN scripted scenario + provider (distinct from the builder's),
    # so its turn emits a plain digest line rather than re-matching a builder
    # CRUD rule. The provider is pre-created here; the builder only references
    # it by id when it calls create_agent.
    digest_scenario = f"scripted:digest-{sfx}"
    digest_provider_id = f"p-digest-{sfx}"
    digest_line = "Digest: generic collections shipped; channel media live; release staged."
    registry.register(digest_scenario, [Rule(emit_text=digest_line)])

    cleanup: list[str] = []
    trigger_id: str | None = None
    try:
        # --- Embedder + SSP so the built collection is real + searchable. ---
        r = await authed_client.post("/v1/ssp", json={"id": ssp_id, **_SSP})
        assert r.status_code in (201, 409), r.text
        cleanup.append(f"/v1/ssp/{ssp_id}")
        r = await authed_client.post(
            "/v1/embedding_providers", json={"id": emb_id, **_EMBED},
        )
        assert r.status_code in (201, 409), r.text
        cleanup.append(f"/v1/embedding_providers/{emb_id}")

        # The digest LLM provider the summarizer agent (created by the builder)
        # will reference. Points at the mock; lists only the digest scenario.
        r = await authed_client.post(
            "/v1/llm_providers",
            json={
                "id": digest_provider_id,
                "provider": "openchat",
                "models": [{"name": digest_scenario, "context_length": 8192}],
                "config": {"url": base_url, "flavor": "lmstudio"},
                "limits": {"max_concurrency": 4},
            },
        )
        assert r.status_code in (200, 201, 409), r.text
        cleanup.append(f"/v1/llm_providers/{digest_provider_id}")

        # The runnable graph the builder will create: begin -> summarizer -> end.
        graph_body = {
            "id": graph_id,
            "description": "Summarize today's news into a one-line digest.",
            "nodes": [
                {"kind": "begin", "id": "begin"},
                {
                    "kind": "agent", "id": "summ",
                    "agent_id": summarizer_id,
                    "input_template": "Summarize today's news into a digest.",
                },
                {"kind": "end", "id": "end",
                 "output_template": "{{ nodes.summ.text }}"},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "summ"},
                {"kind": "static", "from_node": "summ", "to_node": "end"},
            ],
        }

        # --- The scripted app-builder: one CRUD call per turn. Rules are
        # ordered LATEST-FIRST, keyed on a token unique to the previous
        # step's result (see module docstring on the sequencing trap). ---
        builder_tools = [
            "system__create_collection", "system__put_document",
            "system__create_agent", "system__create_graph",
            "trigger__create", "trigger__create_subscription",
            "trigger__fire_now",
        ]
        rules = [
            # after create_graph (result has "nodes") -> create the trigger
            Rule(
                when_last_tool_result_contains='"nodes"',
                emit_tool="trigger__create",
                emit_args={
                    "slug": trigger_slug,
                    "name": "Nightly digest",
                    "config": {"kind": "scheduled", "cron": "0 2 1 1 *",
                               "timezone": "UTC", "catchup": "none"},
                },
                emit_tool_call_id="c5",
            ),
            # after create_agent (result has "system_prompt") -> create graph
            Rule(
                when_last_tool_result_contains='"system_prompt"',
                emit_tool="system__create_graph",
                emit_args={"entity": graph_body},
                emit_tool_call_id="c4",
            ),
            # after put_document (id is 'document-...') -> create the agent
            Rule(
                when_last_tool_result_contains='"document-',
                emit_tool="system__create_agent",
                emit_args={"entity": {
                    "id": summarizer_id,
                    "description": "Summarizes the news into a digest.",
                    "model": {"provider_id": digest_provider_id,
                              "model_name": digest_scenario},
                    "tools": [],
                    "system_prompt": ["Return a one-line digest of the news."],
                }},
                emit_tool_call_id="c3",
            ),
            # after create_collection (result has "embedder") -> seed a doc
            Rule(
                when_last_tool_result_contains='"embedder"',
                emit_tool="system__put_document",
                emit_args={
                    "collection_id": coll_id, "path": _DOC_PATH,
                    "content": _DOC_BODY, "title": "Today",
                },
                emit_tool_call_id="c2",
            ),
            # turn 1: no tool result yet -> create the collection
            Rule(
                when_tool_result=False,
                emit_tool="system__create_collection",
                emit_args={"entity": {
                    "id": coll_id,
                    "description": "News digest knowledge base",
                    "embedder": {"provider_id": emb_id, "model": _EMBED_MODEL},
                    "search_provider_id": ssp_id,
                }},
                emit_tool_call_id="c1",
            ),
            # after trigger create (result has the slug) -> stop
            Rule(
                when_last_tool_result_contains=trigger_slug,
                emit_text="Built the digest app: collection, doc, agent, graph, trigger.",
            ),
        ]
        builder = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"builder-{sfx}",
            scenario=scenario,
            tools=builder_tools,
            system_prompt=[
                "You build a whole mini-app from one request using the CRUD "
                "tools: create the collection, seed a doc, create the agent, "
                "create the graph, then create a scheduled trigger."
            ],
            rules=rules,
        )

        wid = await make_local_workspace(
            authed_client, suffix=f"builder-{sfx}", root=tmp_path,
        )

        # --- Run the builder agent: it provisions the whole app. ---
        r = await authed_client.post(
            f"/v1/workspaces/{wid}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": builder["agent_id"]},
                "initial_instructions": (
                    "Build a nightly news-digest app: create the collection, "
                    "seed a doc, create the summarizer agent, create the digest "
                    "graph, and create a scheduled trigger."
                ),
                "auto_start": True,
            },
        )
        assert r.status_code in (200, 201), r.text
        build_sid = r.json()["id"]
        build_final = await wait_terminal(authed_client, build_sid, timeout_s=120)
        assert build_final.get("status") == "ended", build_final
        assert build_final.get("ended_reason") == "completed", (
            f"the builder agent did not finish provisioning the app: {build_final}"
        )

        # The builder called each distinct CRUD tool exactly in the assembly
        # order (no infinite single-tool loop -- the sequencing guard).
        build_msgs = (
            tmp_path / wid / ".state" / "sessions" / build_sid / "messages.jsonl"
        )
        assert build_msgs.exists(), f"builder wrote no transcript at {build_msgs}"
        called = _builder_tool_calls(build_msgs.read_text(encoding="utf-8"))
        assert called == [
            "system__create_collection",
            "system__put_document",
            "system__create_agent",
            "system__create_graph",
            "trigger__create",
        ], f"builder did not call the CRUD tools in assembly order: {called}"

        # --- (1) Every entity the agent created PERSISTED via REST GET. ---
        for label, url in [
            ("collection", f"/v1/collections/{coll_id}"),
            ("agent", f"/v1/agents/{summarizer_id}"),
            ("graph", f"/v1/graphs/{graph_id}"),
        ]:
            g = await authed_client.get(url)
            assert g.status_code == 200, f"{label} not persisted: {g.text}"

        # The trigger the agent created via trigger__create persisted.
        tg = await authed_client.get("/v1/triggers")
        assert tg.status_code == 200, tg.text
        items = tg.json().get("items", [])
        trigger = next((t for t in items if t.get("slug") == trigger_slug), None)
        assert trigger is not None, (
            f"trigger {trigger_slug!r} not created by the agent: {items}"
        )
        trigger_id = trigger["id"]
        gt = await authed_client.get(f"/v1/triggers/{trigger_id}")
        assert gt.status_code == 200, gt.text

        # --- (2) The seeded document is SEARCHABLE (real embedder + pgvector). ---
        srch = await authed_client.post(
            f"/v1/collections/{coll_id}/search",
            json={"query": "channel media pipeline", "top_k": 5},
        )
        assert srch.status_code == 200, srch.text
        hits = srch.json().get("hits", [])
        assert hits, (
            f"the doc the agent seeded via put_document is not searchable: "
            f"{srch.json()}"
        )

        # --- (3) The assembled app RUNS. Wire the graph_fresh_session
        # subscription (the trigger toolset's create_subscription leg) and
        # fire the trigger once; the fired graph session must run to terminal
        # completed with an on-disk transcript -- proving the built app is
        # runnable, not merely defined. ---
        rs = await authed_client.post(
            f"/v1/triggers/{trigger_id}/subscriptions",
            json={
                "config": {
                    "kind": "graph_fresh_session",
                    "graph_id": graph_id,
                    "workspace_id": wid,
                },
                "payload_template": json.dumps({"run": "manual"}),
                "parallelism": "skip",
            },
        )
        assert rs.status_code in (200, 201), rs.text
        sub_id = rs.json()["id"]

        fr = await authed_client.post(
            f"/v1/triggers/{trigger_id}/fire_now", json={},
        )
        assert fr.status_code == 200, fr.text
        fire = fr.json()
        assert not fire.get("skipped"), fire
        run_sid = _dispatched_session_id(fire)
        assert run_sid is not None, f"fire_now dispatched no graph session: {fire}"

        run_final = await wait_terminal(authed_client, run_sid, timeout_s=120)
        assert run_final.get("status") == "ended", run_final
        assert run_final.get("ended_reason") == "completed", (
            f"the assembled app's fired graph did not run to completion: "
            f"{run_final}"
        )
        assert (run_final.get("metadata") or {}).get("subscription_id") == sub_id, (
            f"the fired graph session was not tagged with the subscription id: "
            f"{run_final}"
        )

        run_msgs = (
            tmp_path / wid / ".state" / "sessions" / run_sid / "messages.jsonl"
        )
        run_state = (
            tmp_path / wid / ".state" / "graphs" / run_sid / "state.json"
        )
        assert run_msgs.exists(), (
            f"the fired graph wrote no on-disk transcript at {run_msgs} -- the "
            f"assembled app did not actually run"
        )
        assert run_state.exists(), (
            f"the fired graph wrote no on-disk state at {run_state}"
        )
        state = json.loads(run_state.read_text(encoding="utf-8"))
        assert state.get("ended_reason") == "completed", state
    finally:
        # Best-effort cleanup of the entities the builder created. Deleting
        # the trigger cascades its subscription; then the graph/agent/collection
        # the agent provisioned; then the embedder + SSP. Cleanup is wrapped so
        # a transient delete error never masks the test's real verdict.
        import contextlib

        urls: list[str] = []
        if trigger_id is not None:
            urls.append(f"/v1/triggers/{trigger_id}")
        urls += [
            f"/v1/graphs/{graph_id}",
            f"/v1/agents/{summarizer_id}",
            f"/v1/collections/{coll_id}",
        ]
        urls += list(reversed(cleanup))
        for url in urls:
            with contextlib.suppress(Exception):
                await authed_client.delete(url)
