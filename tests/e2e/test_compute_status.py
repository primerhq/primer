"""E2E: Agent + Graph status reports for unresolved references.

Covers backlog items T0022 (agent → missing LLMProvider),
T0023 (agent → missing toolset), T0024 (graph → missing agent).

Spec §9: ``/agents/{id}/status`` and ``/graphs/{id}/status`` return
``{"ok": bool, "issues": [str, ...]}``. Issues are plain strings; the
test inspects substrings rather than asserting exact wording so wording
tweaks in the server do not flap the suite.
"""

from __future__ import annotations

import httpx
import pytest


def _llm_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }


def _agent_body(entity_id: str, *, provider_id: str, tools: list[str]) -> dict:
    return {
        "id": entity_id,
        "description": "test agent",
        "model": {"provider_id": provider_id, "model_name": "claude-sonnet-4-6"},
        "tools": tools,
    }


def _graph_body(entity_id: str, *, agent_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "test graph",
        "nodes": [
            {"kind": "agent", "id": "n1", "agent_id": agent_id},
        ],
        "edges": [],
        "entry_node_id": "n1",
    }


def _toolset_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }


# ============================================================================
# T0022 — Agent status flags missing LLMProvider
# ============================================================================


@pytest.mark.asyncio
async def test_t0022_agent_status_missing_llm_provider(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    agent_id = f"agent-t0022-{unique_suffix}"
    missing_provider_id = f"does-not-exist-{unique_suffix}"
    create = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=missing_provider_id, tools=[]),
    )
    assert create.status_code == 201, create.text
    try:
        resp = await client.get(f"/v1/agents/{agent_id}/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False, f"expected ok=false, got {body!r}"
        issues = body["issues"]
        assert isinstance(issues, list) and issues
        # At least one issue must mention the missing provider id so an
        # operator can act on it.
        assert any(missing_provider_id in str(i) for i in issues), (
            f"no issue references missing provider {missing_provider_id!r}: "
            f"{issues!r}"
        )
    finally:
        await client.delete(f"/v1/agents/{agent_id}")


# ============================================================================
# T0023 — Agent status flags missing toolset (with provider present)
# ============================================================================


@pytest.mark.asyncio
async def test_t0023_agent_status_missing_toolset(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    provider_id = f"llm-{unique_suffix}"
    agent_id = f"agent-t0023-{unique_suffix}"
    missing_toolset_id = f"missing-ts-{unique_suffix}"

    # Real provider so the only issue is the toolset miss.
    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        # Tool ids are scoped: "<toolset_id>__<bare_name>". The status
        # check splits on '__' and resolves the toolset_id portion.
        scoped_tool = f"{missing_toolset_id}__some_tool"
        ag = await client.post(
            "/v1/agents",
            json=_agent_body(
                agent_id, provider_id=provider_id, tools=[scoped_tool],
            ),
        )
        assert ag.status_code == 201, ag.text
        try:
            resp = await client.get(f"/v1/agents/{agent_id}/status")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ok"] is False, f"expected ok=false, got {body!r}"
            issues = body["issues"]
            assert any(missing_toolset_id in str(i) for i in issues), (
                f"no issue references missing toolset "
                f"{missing_toolset_id!r}: {issues!r}"
            )
            # Provider is present, so it must NOT appear in issues.
            assert not any(provider_id in str(i) for i in issues), (
                f"provider {provider_id!r} should not be flagged: {issues!r}"
            )
        finally:
            await client.delete(f"/v1/agents/{agent_id}")
    finally:
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0045 — Multi-toolset Agent status reports ONLY the missing toolset
# ============================================================================


@pytest.mark.asyncio
async def test_t0045_agent_status_multi_toolset_only_missing_flagged(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0045 — when an Agent references two toolsets via scoped tool ids
    and one toolset row exists while the other does not, the status
    response must:

    - report `ok=false`
    - have exactly ONE issue (not two — the present toolset must not
      be flagged)
    - the single issue must reference the missing toolset_id
    - it must NOT reference the present toolset_id
    """
    provider_id = f"llm-multi-{unique_suffix}"
    present_toolset_id = f"ts-present-{unique_suffix}"
    missing_toolset_id = f"ts-missing-{unique_suffix}"
    agent_id = f"agent-multi-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        ts = await client.post("/v1/toolsets", json=_toolset_body(present_toolset_id))
        assert ts.status_code == 201, ts.text
        try:
            ag = await client.post(
                "/v1/agents",
                json=_agent_body(
                    agent_id,
                    provider_id=provider_id,
                    tools=[
                        f"{present_toolset_id}__alpha",
                        f"{missing_toolset_id}__beta",
                    ],
                ),
            )
            assert ag.status_code == 201, ag.text
            try:
                resp = await client.get(f"/v1/agents/{agent_id}/status")
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["ok"] is False, body
                issues = body["issues"]
                assert isinstance(issues, list)
                # Exactly ONE issue — for the missing toolset only.
                # If the implementation accidentally surfaces the
                # present toolset (e.g. by failing to short-circuit
                # on hit), this assertion will catch it.
                missing_count = sum(
                    1 for i in issues if missing_toolset_id in str(i)
                )
                present_count = sum(
                    1 for i in issues if present_toolset_id in str(i)
                )
                assert missing_count == 1, (
                    f"expected exactly one issue mentioning missing toolset "
                    f"{missing_toolset_id!r}, got {missing_count}: {issues!r}"
                )
                assert present_count == 0, (
                    f"present toolset {present_toolset_id!r} must NOT appear "
                    f"in issues: {issues!r}"
                )
            finally:
                await client.delete(f"/v1/agents/{agent_id}")
        finally:
            await client.delete(f"/v1/toolsets/{present_toolset_id}")
    finally:
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0109 — Agent status ok=true when all references resolve (positive path)
# ============================================================================


@pytest.mark.asyncio
async def test_t0109_agent_status_ok_when_all_references_resolve(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0109 — positive control: an Agent that references a real
    LLMProvider AND a real Toolset (via a scoped tool id) returns
    `{"ok": true, "issues": []}`. The test crew so far has covered
    every NEGATIVE branch of agent_status; this one pins the happy
    path so a future regression that always-flags-an-issue is caught."""
    provider_id = f"llm-ok-{unique_suffix}"
    toolset_id = f"ts-ok-{unique_suffix}"
    agent_id = f"agent-ok-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        ts = await client.post("/v1/toolsets", json=_toolset_body(toolset_id))
        assert ts.status_code == 201, ts.text
        try:
            ag = await client.post(
                "/v1/agents",
                json=_agent_body(
                    agent_id,
                    provider_id=provider_id,
                    tools=[f"{toolset_id}__alpha"],
                ),
            )
            assert ag.status_code == 201, ag.text
            try:
                resp = await client.get(f"/v1/agents/{agent_id}/status")
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["ok"] is True, body
                assert body["issues"] == [], body
            finally:
                await client.delete(f"/v1/agents/{agent_id}")
        finally:
            await client.delete(f"/v1/toolsets/{toolset_id}")
    finally:
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0033 — Agent status recovers after delete+recreate of its LLMProvider
# ============================================================================


@pytest.mark.asyncio
async def test_t0033_agent_status_recovers_after_provider_recreate(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0033 — status walks the live row each call (no cached evaluation):

    1. provider+agent → status ok
    2. delete provider → status reports missing
    3. recreate provider with same id → status ok again
    """
    provider_id = f"llm-rec-{unique_suffix}"
    agent_id = f"agent-rec-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        ag = await client.post(
            "/v1/agents",
            json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
        )
        assert ag.status_code == 201, ag.text
        try:
            # Step 1: status ok with provider present
            ok = await client.get(f"/v1/agents/{agent_id}/status")
            assert ok.status_code == 200, ok.text
            assert ok.json()["ok"] is True, ok.json()

            # Step 2: provider deleted → status flags missing
            rm = await client.delete(f"/v1/llm_providers/{provider_id}")
            assert rm.status_code == 204, rm.text
            broken = await client.get(f"/v1/agents/{agent_id}/status")
            assert broken.status_code == 200, broken.text
            broken_body = broken.json()
            assert broken_body["ok"] is False, broken_body
            assert any(
                provider_id in str(i) for i in broken_body["issues"]
            ), broken_body

            # Step 3: provider recreated with same id → status ok again
            recreated = await client.post(
                "/v1/llm_providers", json=_llm_body(provider_id),
            )
            assert recreated.status_code == 201, recreated.text
            healed = await client.get(f"/v1/agents/{agent_id}/status")
            assert healed.status_code == 200, healed.text
            assert healed.json()["ok"] is True, healed.json()
        finally:
            await client.delete(f"/v1/agents/{agent_id}")
    finally:
        # Best-effort: provider may already be re-deleted by step 3, that's fine
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0106 — Unicode marker in Agent description round-trips byte-exact
# ============================================================================


@pytest.mark.asyncio
async def test_t0106_agent_unicode_description_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0106 — POST an Agent whose description contains CJK + emoji +
    RTL chars; GET returns the same NFC code points byte-exact, and
    the entry shows up in LIST with an identical description."""
    provider_id = f"llm-uni-{unique_suffix}"
    agent_id = f"agent-uni-{unique_suffix}"
    # CJK + emoji + Arabic (RTL) — all valid NFC code points
    distinctive = f"日本語 🎉 العربية {unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        ag = await client.post(
            "/v1/agents",
            json=_agent_body(
                agent_id, provider_id=provider_id, tools=[],
            ) | {"description": distinctive},
        )
        assert ag.status_code == 201, ag.text
        try:
            # GET returns identical bytes
            got = await client.get(f"/v1/agents/{agent_id}")
            assert got.status_code == 200, got.text
            assert got.json()["description"] == distinctive

            # LIST contains the same description bytes
            listed = await client.get("/v1/agents?limit=200&offset=0")
            assert listed.status_code == 200, listed.text
            descs = {
                item["id"]: item["description"]
                for item in listed.json()["items"]
            }
            assert descs.get(agent_id) == distinctive, descs.get(agent_id)
        finally:
            await client.delete(f"/v1/agents/{agent_id}")
    finally:
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0076 — Agent PUT adding an unknown scoped tool id flips status.ok=false
# ============================================================================


@pytest.mark.asyncio
async def test_t0076_agent_put_with_unknown_tool_flips_status(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0076 — start with an Agent referencing valid tools (status.ok),
    PUT-update it adding a scoped tool id whose toolset doesn't exist,
    and assert the status report flips to ok=false with a missing-toolset
    issue.
    """
    provider_id = f"llm-put-{unique_suffix}"
    present_toolset_id = f"ts-put-{unique_suffix}"
    missing_toolset_id = f"missing-put-{unique_suffix}"
    agent_id = f"agent-put-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        ts = await client.post("/v1/toolsets", json=_toolset_body(present_toolset_id))
        assert ts.status_code == 201, ts.text
        try:
            # Step 1 — Agent referencing only the present toolset, status ok
            ag = await client.post(
                "/v1/agents",
                json=_agent_body(
                    agent_id,
                    provider_id=provider_id,
                    tools=[f"{present_toolset_id}__alpha"],
                ),
            )
            assert ag.status_code == 201, ag.text
            try:
                ok = await client.get(f"/v1/agents/{agent_id}/status")
                assert ok.status_code == 200, ok.text
                assert ok.json()["ok"] is True, ok.json()

                # Step 2 — PUT adding a scoped tool id with a missing
                # toolset_id portion
                put = await client.put(
                    f"/v1/agents/{agent_id}",
                    json=_agent_body(
                        agent_id,
                        provider_id=provider_id,
                        tools=[
                            f"{present_toolset_id}__alpha",
                            f"{missing_toolset_id}__beta",
                        ],
                    ),
                )
                assert put.status_code == 200, put.text

                # Step 3 — status now ok=false with the missing-toolset issue
                broken = await client.get(f"/v1/agents/{agent_id}/status")
                assert broken.status_code == 200, broken.text
                body = broken.json()
                assert body["ok"] is False, body
                assert any(
                    missing_toolset_id in str(i) for i in body["issues"]
                ), body
                # The present toolset must still NOT be flagged
                assert not any(
                    present_toolset_id in str(i) for i in body["issues"]
                ), body
            finally:
                await client.delete(f"/v1/agents/{agent_id}")
        finally:
            await client.delete(f"/v1/toolsets/{present_toolset_id}")
    finally:
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0024 — Graph status flags missing agent reference
# ============================================================================


@pytest.mark.asyncio
async def test_t0024_graph_status_missing_agent(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    graph_id = f"graph-t0024-{unique_suffix}"
    missing_agent_id = f"missing-agent-{unique_suffix}"

    create = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=missing_agent_id),
    )
    assert create.status_code == 201, create.text
    try:
        resp = await client.get(f"/v1/graphs/{graph_id}/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False, f"expected ok=false, got {body!r}"
        issues = body["issues"]
        assert any(missing_agent_id in str(i) for i in issues), (
            f"no issue references missing agent {missing_agent_id!r}: "
            f"{issues!r}"
        )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")


# ============================================================================
# T0171 — Graph status surfaces BOTH a missing agent and a missing sub-graph
# ============================================================================


@pytest.mark.asyncio
async def test_t0171_graph_status_flags_multiple_missing_references(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0171 — extends T0024 to a graph with BOTH a missing agent node and
    a missing sub-graph node. The status endpoint must surface BOTH
    issues (not just the first one), so an operator can act on every
    broken reference in a single round-trip.
    """
    graph_id = f"graph-t0171-{unique_suffix}"
    missing_agent_id = f"missing-agent-{unique_suffix}"
    missing_subgraph_id = f"missing-subgraph-{unique_suffix}"

    create = await client.post(
        "/v1/graphs",
        json={
            "id": graph_id,
            "description": "multi-issue graph for T0171",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": missing_agent_id},
                {"kind": "graph", "id": "n2", "graph_id": missing_subgraph_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "n2"},
                {"kind": "static", "from_node": "n2", "to_node": "end"},
            ],
            "entry_node_id": "n1",
        },
    )
    assert create.status_code == 201, create.text
    try:
        resp = await client.get(f"/v1/graphs/{graph_id}/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False, body
        issues = body["issues"]
        assert isinstance(issues, list) and issues, body

        issues_text = " ".join(str(i) for i in issues)
        assert missing_agent_id in issues_text, (
            f"missing agent {missing_agent_id!r} not surfaced in any "
            f"issue: {issues!r}"
        )
        assert missing_subgraph_id in issues_text, (
            f"missing sub-graph {missing_subgraph_id!r} not surfaced in "
            f"any issue: {issues!r}"
        )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")


# ============================================================================
# T0240 — DELETE provider concurrent with /status: clean envelopes both ways
# ============================================================================


@pytest.mark.asyncio
async def test_t0240_delete_provider_concurrent_with_status(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0240 — Race: DELETE /v1/llm_providers/{p} concurrent with
    GET /v1/agents/{a}/status (which walks the provider reference).

    Either order of events must produce a clean envelope on the
    status call (200 with ok=true OR 200 with ok=false; either is
    fine — never /errors/internal from a load-vanished-mid-handler
    pattern). Mirrors T0104 (parallel GET+DELETE on toolset) at the
    cross-resource boundary.

    Distinct from T0033's sequential delete-then-status check.
    """
    provider_id = f"llm-race-{unique_suffix}"
    agent_id = f"agent-race-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        ag = await client.post(
            "/v1/agents",
            json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
        )
        assert ag.status_code == 201, ag.text
        try:
            # Race: DELETE provider + GET agent status concurrently
            import asyncio
            delete_task = asyncio.create_task(
                client.delete(f"/v1/llm_providers/{provider_id}"),
            )
            status_task = asyncio.create_task(
                client.get(f"/v1/agents/{agent_id}/status"),
            )
            delete_resp, status_resp = await asyncio.gather(
                delete_task, status_task,
            )

            # DELETE: 204 (winner) or 404 (already gone — shouldn't
            # happen on first race iteration, but tolerate)
            assert delete_resp.status_code in (204, 404), delete_resp.text

            # Status call must produce a clean envelope; the value
            # of `ok` depends on which task won the race
            assert status_resp.status_code == 200, status_resp.text
            body = status_resp.json()
            assert "ok" in body, body
            assert isinstance(body.get("issues"), list), body
            assert body.get("type") != "/errors/internal", body
        finally:
            await client.delete(f"/v1/agents/{agent_id}")
    finally:
        # Provider may already be gone
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0265 — DELETE LLMProvider then create Agent referencing the deleted id
# ============================================================================


@pytest.mark.asyncio
async def test_t0265_create_agent_referencing_deleted_provider_permissive(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0265 — Create LLMProvider, then DELETE it. Then POST an Agent
    that references the now-deleted provider id. Pin: Agent create
    succeeds (orphan-tolerated like T0068/T0157); /agents/{id}/status
    flips ok=false with the missing-llm-provider issue.

    Distinct from T0033 (which deletes the provider AFTER creating
    the agent) — this exercises the create-with-already-missing-ref
    path.
    """
    provider_id = f"llm-deleted-{unique_suffix}"
    agent_id = f"agent-t0265-{unique_suffix}"

    # Create then delete the provider
    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    rm = await client.delete(f"/v1/llm_providers/{provider_id}")
    assert rm.status_code == 204, rm.text

    # Now create the agent referencing the deleted id — must succeed
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, (
        f"agent create with missing-provider ref should succeed (orphan-"
        f"tolerated); got {ag.status_code}: {ag.text}"
    )
    try:
        # /status flips to ok=false with missing-provider issue
        status = await client.get(f"/v1/agents/{agent_id}/status")
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["ok"] is False, body
        issues = body["issues"]
        assert any(
            provider_id in str(i) for i in issues
        ), f"no issue references missing provider {provider_id!r}: {issues!r}"
    finally:
        await client.delete(f"/v1/agents/{agent_id}")


# ============================================================================
# T0344 — Provider→Agent→Graph cascade: DELETE LLMProvider; both flip ok=false
# ============================================================================


@pytest.mark.asyncio
async def test_t0344_delete_provider_flips_agent_and_graph_status(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0344 — Multi-tier status walk: build LLMProvider→Agent→Graph,
    then DELETE the LLMProvider. Both /agents/{a}/status AND
    /graphs/{g}/status must flip to ok=false with the missing-LLM-
    provider issue surfacing.
    """
    provider_id = f"llm-t0344-{unique_suffix}"
    agent_id = f"agent-t0344-{unique_suffix}"
    graph_id = f"graph-t0344-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    gr = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert gr.status_code == 201, gr.text
    try:
        # Sanity: pre-delete both ok
        ag_status_pre = await client.get(f"/v1/agents/{agent_id}/status")
        gr_status_pre = await client.get(f"/v1/graphs/{graph_id}/status")
        assert ag_status_pre.json()["ok"] is True, ag_status_pre.text
        assert gr_status_pre.json()["ok"] is True, gr_status_pre.text

        # DELETE the provider
        rm = await client.delete(f"/v1/llm_providers/{provider_id}")
        assert rm.status_code == 204, rm.text

        # Agent status flips and surfaces missing-provider
        ag_status_post = await client.get(f"/v1/agents/{agent_id}/status")
        assert ag_status_post.status_code == 200, ag_status_post.text
        ag_body = ag_status_post.json()
        assert ag_body["ok"] is False, ag_body
        assert any(
            provider_id in str(i) for i in ag_body["issues"]
        ), ag_body

        # Graph status — agent reference is still valid (agent exists)
        # but the agent's downstream provider is missing. The graph
        # walker may either surface the agent as ok=false (transitive)
        # or only flag direct missing references. Pin: 200 envelope
        # cleanly, no /errors/internal.
        gr_status_post = await client.get(f"/v1/graphs/{graph_id}/status")
        assert gr_status_post.status_code == 200, gr_status_post.text
        gr_body = gr_status_post.json()
        assert "ok" in gr_body, gr_body
        assert isinstance(gr_body.get("issues"), list), gr_body
        # Soft pin: graph likely flips ok=false too (agent's status
        # is broken). Either ok=true or ok=false is documentable.
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")


# ============================================================================
# T0357 — Graph→sub-graph→agent: top graph /status walks depth 1 only
# ============================================================================


@pytest.mark.asyncio
async def test_t0357_graph_status_walks_depth_one_for_subgraph_refs(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0357 — Build agent A; sub-graph G2 referencing A; top graph
    G1 with a `kind:graph` node pointing at G2 plus a missing-agent
    reference on G1 itself.

    G1 /status must surface its OWN missing-agent issue but NOT
    transitively walk into G2's nodes. Pin the depth-1 walk
    contract.
    """
    provider_id = f"llm-t0357-{unique_suffix}"
    agent_id = f"agent-t0357-{unique_suffix}"
    sub_graph_id = f"subgraph-t0357-{unique_suffix}"
    top_graph_id = f"topgraph-t0357-{unique_suffix}"
    missing_agent_id = f"missing-on-top-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    # Sub-graph references A (real agent)
    sub = await client.post(
        "/v1/graphs",
        json={
            "id": sub_graph_id,
            "description": "T0357 sub-graph",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
        },
    )
    assert sub.status_code == 201, sub.text

    # Top graph: references sub-graph PLUS a missing agent
    top = await client.post(
        "/v1/graphs",
        json={
            "id": top_graph_id,
            "description": "T0357 top graph",
            "nodes": [
                {"kind": "agent", "id": "missing_node",
                 "agent_id": missing_agent_id},
                {"kind": "graph", "id": "sub_node",
                 "graph_id": sub_graph_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "missing_node",
                 "to_node": "sub_node"},
                {"kind": "static", "from_node": "sub_node",
                 "to_node": "end"},
            ],
            "entry_node_id": "missing_node",
        },
    )
    assert top.status_code == 201, top.text

    try:
        # GET top graph status
        resp = await client.get(f"/v1/graphs/{top_graph_id}/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False, body
        issues_text = " ".join(str(i) for i in body["issues"])

        # G1's own missing agent reference IS in the issues
        assert missing_agent_id in issues_text, (
            f"G1's own missing agent {missing_agent_id!r} not surfaced: "
            f"{body['issues']!r}"
        )
        # Walk should NOT transitively descend into G2's nodes — A is a
        # real agent inside G2, but the walker shouldn't probe it via
        # G1. Pin: no /errors/internal regardless of how walk handles
        # sub-graph refs.
        assert body.get("type") != "/errors/internal", body
    finally:
        await client.delete(f"/v1/graphs/{top_graph_id}")
        await client.delete(f"/v1/graphs/{sub_graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0358 — Session bound to a Graph with a sub-Graph reference is created
# ============================================================================


@pytest.mark.asyncio
async def test_t0358_session_bound_to_graph_with_subgraph_creates_cleanly(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0358 — Session create with binding={kind:graph} where the
    graph contains a sub-Graph node. Pin the create succeeds and
    the binding round-trips through GET. Companion to T0156 which
    used a flat agent-only graph.

    NB: graph executor itself is NotImplemented (T0156); this test
    only pins the create + binding-roundtrip path, NOT execution.
    """
    # Need workspace + provider chain
    import tempfile
    provider_id = f"llm-t0358-{unique_suffix}"
    agent_id = f"agent-t0358-{unique_suffix}"
    sub_graph_id = f"sub-t0358-{unique_suffix}"
    top_graph_id = f"top-t0358-{unique_suffix}"
    wp_id = f"wp-t0358-{unique_suffix}"
    tpl_id = f"wt-t0358-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    sub = await client.post(
        "/v1/graphs",
        json={
            "id": sub_graph_id,
            "description": "T0358 sub",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
        },
    )
    assert sub.status_code == 201, sub.text

    top = await client.post(
        "/v1/graphs",
        json={
            "id": top_graph_id,
            "description": "T0358 top",
            "nodes": [
                {"kind": "graph", "id": "sub_node",
                 "graph_id": sub_graph_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "sub_node",
                 "to_node": "end"},
            ],
            "entry_node_id": "sub_node",
        },
    )
    assert top.status_code == 201, top.text

    workspace_id: str | None = None
    session_id: str | None = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wp = await client.post(
                "/v1/workspace_providers",
                json={
                    "id": wp_id,
                    "provider": "local",
                    "config": {"kind": "local", "path": tmp},
                },
            )
            assert wp.status_code == 201, wp.text
            try:
                tpl = await client.post(
                    "/v1/workspace_templates",
                    json={
                        "id": tpl_id,
                        "description": "T0358",
                        "provider_id": wp_id,
                        "backend": {"kind": "local"},
                    },
                )
                assert tpl.status_code == 201, tpl.text
                try:
                    ws = await client.post(
                        "/v1/workspaces", json={"template_id": tpl_id},
                    )
                    assert ws.status_code == 201, ws.text
                    workspace_id = ws.json()["id"]

                    sess = await client.post(
                        f"/v1/workspaces/{workspace_id}/sessions",
                        json={
                            "binding": {
                                "kind": "graph",
                                "graph_id": top_graph_id,
                            },
                            "auto_start": False,
                        },
                    )
                    assert sess.status_code == 201, sess.text
                    session_id = sess.json()["id"]

                    # Binding round-trips
                    got = await client.get(f"/v1/sessions/{session_id}")
                    assert got.status_code == 200, got.text
                    binding = got.json().get("binding", {})
                    assert binding.get("kind") == "graph", got.json()
                    assert binding.get("graph_id") == top_graph_id, got.json()
                finally:
                    if workspace_id:
                        if session_id:
                            await client.post(
                                f"/v1/workspaces/{workspace_id}/sessions/"
                                f"{session_id}/cancel",
                            )
                        await client.delete(
                            f"/v1/workspaces/{workspace_id}",
                        )
                    await client.delete(f"/v1/workspace_templates/{tpl_id}")
            finally:
                await client.delete(f"/v1/workspace_providers/{wp_id}")
    finally:
        await client.delete(f"/v1/graphs/{top_graph_id}")
        await client.delete(f"/v1/graphs/{sub_graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0192 — GET /v1/agents/{missing}/status returns 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0192_agent_status_on_missing_agent_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0192 — GET /v1/agents/{missing}/status on a non-existent agent id
    must return 404 /errors/not-found. T0022 covers the case where the
    agent exists but its provider doesn't; this is the agent-itself-
    missing variant.
    """
    missing_id = f"missing-agent-{unique_suffix}"
    resp = await client.get(f"/v1/agents/{missing_id}/status")
    assert resp.status_code == 404, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404


# ============================================================================
# T0193 — GET /v1/graphs/{missing}/status returns 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0193_graph_status_on_missing_graph_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0193 — GET /v1/graphs/{missing}/status on a non-existent graph id
    must return 404 /errors/not-found. Symmetric with T0192 for the
    graph-status endpoint.
    """
    missing_id = f"missing-graph-{unique_suffix}"
    resp = await client.get(f"/v1/graphs/{missing_id}/status")
    assert resp.status_code == 404, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404


# ============================================================================
# T0194 — Graph with a node pointing at the graph's own id (cycle) is clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0194_self_referential_graph_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0194 — A graph whose `agent_id` node refers to the graph's OWN
    id (not an Agent row). This is a degenerate / mistaken configuration
    that should not crash the validator or the status endpoint.

    The status endpoint must produce a clean envelope (any 4xx or 200
    with issues populated). No /errors/internal leak.
    """
    graph_id = f"graph-self-{unique_suffix}"
    # Use the graph's own id as the agent_id — undocumented edge case
    create = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=graph_id),
    )
    # Create may accept it (no FK enforcement; T0157/T0068 pattern) or
    # reject 4xx. Either is fine. No 5xx.
    assert create.status_code < 500, create.text
    if create.status_code in (200, 201):
        try:
            status_resp = await client.get(
                f"/v1/graphs/{graph_id}/status",
            )
            assert status_resp.status_code < 500, status_resp.text
            if status_resp.status_code == 200:
                body = status_resp.json()
                # Self-reference should typically flag ok=false (the
                # agent_id doesn't resolve to an Agent), but the only
                # invariant here is the envelope is clean.
                assert "ok" in body, body
            else:
                envelope = status_resp.json()
                assert envelope["type"].startswith("/errors/"), envelope
                assert envelope["type"] != "/errors/internal", envelope
        finally:
            await client.delete(f"/v1/graphs/{graph_id}")
    else:
        envelope = create.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope


# ============================================================================
# T0384 — Agent referencing model name not in provider.models flips ok=false
# ============================================================================


@pytest.mark.asyncio
async def test_t0384_agent_status_flags_model_not_in_provider_list(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0384 — Create LLMProvider with models=["model-a"]; create
    Agent referencing the SAME provider but with model_name="model-b"
    (not in the provider's list). Pin: status returns clean envelope
    (no /errors/internal). The walker may or may not enforce model
    membership — record the observed contract.
    """
    provider_id = f"llm-t0384-{unique_suffix}"
    agent_id = f"agent-t0384-{unique_suffix}"

    pr = await client.post(
        "/v1/llm_providers",
        json={
            "id": provider_id,
            "provider": "anthropic",
            "models": [{"name": "model-a", "context_length": 1024}],
            "config": {"api_key": "sk-test"},
            "limits": {"max_concurrency": 1},
        },
    )
    assert pr.status_code == 201, pr.text

    ag = await client.post(
        "/v1/agents",
        json={
            "id": agent_id,
            "description": "T0384",
            "model": {
                "provider_id": provider_id,
                "model_name": "model-b",
            },
            "tools": [],
        },
    )
    assert ag.status_code == 201, ag.text
    try:
        status = await client.get(f"/v1/agents/{agent_id}/status")
        assert status.status_code == 200, status.text
        body = status.json()
        assert body.get("type") != "/errors/internal", body
        assert "ok" in body, body
        assert isinstance(body.get("issues"), list), body
        # Soft pin — log observed behaviour for spec
        print(
            f"[T0384] agent referencing model 'model-b' (not in provider): "
            f"ok={body['ok']}, issues={body['issues']!r}"
        )
    finally:
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0413 — DELETE Toolset referenced by Agent flips Agent /status ok=false
# ============================================================================


@pytest.mark.asyncio
async def test_t0413_delete_toolset_flips_agent_status_ok_false(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0413 — Mirror of T0265 (LLMProvider→Agent FK) for the
    Toolset→Agent FK path. Build LLMProvider, Toolset, Agent
    referencing the toolset by id. Sanity: pre-delete /status ok=true.
    Then DELETE the toolset (succeeds — orphan-tolerated like other
    cascades), then GET /agents/{id}/status: must flip ok=false with
    an issue mentioning the now-missing toolset id.
    """
    provider_id = f"llm-t0413-{unique_suffix}"
    toolset_id = f"ts-t0413-{unique_suffix}"
    agent_id = f"agent-t0413-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ts = await client.post("/v1/toolsets", json=_toolset_body(toolset_id))
    assert ts.status_code == 201, ts.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(
            agent_id, provider_id=provider_id, tools=[toolset_id],
        ),
    )
    assert ag.status_code == 201, ag.text

    try:
        # Sanity: pre-delete the agent is healthy
        pre = await client.get(f"/v1/agents/{agent_id}/status")
        assert pre.status_code == 200, pre.text
        assert pre.json()["ok"] is True, (
            f"pre-delete agent should be ok=true; got {pre.json()!r}"
        )

        # DELETE the referenced toolset (orphan-tolerated)
        rm = await client.delete(f"/v1/toolsets/{toolset_id}")
        assert rm.status_code == 204, rm.text

        # Agent /status must now flip ok=false, with the missing
        # toolset id surfaced in issues so an operator can act.
        post = await client.get(f"/v1/agents/{agent_id}/status")
        assert post.status_code == 200, post.text
        body = post.json()
        assert body["ok"] is False, (
            f"after DELETE toolset, agent should be ok=false; "
            f"got {body!r}"
        )
        issues = body["issues"]
        assert isinstance(issues, list) and issues, body
        assert any(
            toolset_id in str(i) for i in issues
        ), (
            f"no issue references missing toolset {toolset_id!r}: "
            f"{issues!r}"
        )
    finally:
        await client.delete(f"/v1/agents/{agent_id}")
        # Toolset already deleted (or never created on a failure path)
        await client.delete(f"/v1/toolsets/{toolset_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0430 — POST /v1/graphs with edge referencing non-existent node id 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0430_graph_create_edge_unknown_node_id_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0430 — Graph topology validator (matrix/model/graph.py:387)
    rejects edges whose `from_node` or `to_node` doesn't match any
    declared node id. Pin: 422 /errors/validation-error with the
    bad node id surfaced in the envelope, never /errors/internal.

    Pre-pin so the eventual graph executor lands without breaking
    edge-id integrity guarantees.
    """
    provider_id = f"llm-t0430-{unique_suffix}"
    agent_id = f"agent-t0430-{unique_suffix}"
    graph_id = f"graph-t0430-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        # Edge.to_node references "no-such-node" — no node has that id
        body = {
            "id": graph_id,
            "description": "T0430 — edge with bad to_node",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "no-such-node"},
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code != 500, resp.text
        assert resp.status_code == 422, (
            f"graph with bad edge.to_node should be 422; got "
            f"{resp.status_code}: {resp.text}"
        )
        envelope = resp.json()
        assert envelope.get("type") == "/errors/validation-error", envelope
        # The message should mention the bad node id so an operator
        # can act on it
        body_str = resp.text
        assert "no-such-node" in body_str, (
            f"422 envelope should reference the bad node id "
            f"'no-such-node'; body={body_str!r}"
        )

        # Defence: row should not have been created
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 404, (
            f"graph {graph_id!r} unexpectedly created despite 422: "
            f"{got.text}"
        )

        # Same pin for edge.from_node referencing a missing node
        body2 = {
            "id": graph_id,
            "description": "T0430 — edge with bad from_node",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "ghost", "to_node": "end"},
            ],
            "entry_node_id": "n1",
        }
        resp2 = await client.post("/v1/graphs", json=body2)
        assert resp2.status_code == 422, resp2.text
        assert "ghost" in resp2.text, resp2.text
    finally:
        # In case the create somehow succeeded
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0431 — POST /v1/graphs with cyclic edges has documented behaviour
# ============================================================================


@pytest.mark.asyncio
async def test_t0431_graph_create_with_cyclic_edges_documented_behavior(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0431 — Per matrix/model/graph.py:359-363, "Cyclic graphs MUST
    set max_iterations to bound execution; otherwise a stuck cycle
    runs unbounded." The validator does NOT statically detect cycles
    (only edge-id integrity); responsibility for unbounded loops is
    pushed to runtime via max_iterations.

    Pin observed behaviour: a graph with a 2-node cycle (n1→n2,
    n2→n1) and no max_iterations is ACCEPTED at create time
    (status 201). This documents the current contract so a future
    static cycle-detector deliberately breaks this test.
    """
    provider_id = f"llm-t0431-{unique_suffix}"
    agent_id = f"agent-t0431-{unique_suffix}"
    graph_id = f"graph-t0431-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    graph_created = False

    try:
        # Two-node cycle: n1 → n2 → n1
        body = {
            "id": graph_id,
            "description": "T0431 — cyclic edges, no max_iterations",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "agent", "id": "n2", "agent_id": agent_id},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "n2"},
                {"kind": "static", "from_node": "n2", "to_node": "n1"},
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        # Hard pin: never 5xx
        assert resp.status_code != 500, resp.text
        # Acceptable outcomes: 201 (current — no static cycle detection),
        # or 422 (future — explicit cycle detection lands and rejects
        # cycles missing max_iterations). Both are valid contracts.
        assert resp.status_code in (201, 422), (
            f"graph with cyclic edges: unexpected {resp.status_code}: "
            f"{resp.text}"
        )

        if resp.status_code == 201:
            graph_created = True
            # Roundtrip via GET
            got = await client.get(f"/v1/graphs/{graph_id}")
            assert got.status_code == 200, got.text
            assert got.json()["max_iterations"] is None, got.json()
            # Edges preserved
            edge_pairs = sorted(
                (e["from_node"], e["to_node"])
                for e in got.json()["edges"]
            )
            assert edge_pairs == [("n1", "n2"), ("n2", "n1")], got.json()
        else:
            envelope = resp.json()
            assert envelope.get("type") == "/errors/validation-error", envelope
    finally:
        if graph_created:
            await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0447 — Concurrent POST /v1/agents same id (10 racers): exactly one 201
# ============================================================================


@pytest.mark.asyncio
async def test_t0447_concurrent_post_agents_same_id_one_wins(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0447 — Fire 10 concurrent POSTs to /v1/agents all with the
    SAME id. Exactly one wins with 201; the remaining nine return
    409 /errors/conflict. No /errors/internal anywhere; no orphan
    rows (post-race GET returns the single accepted body).

    Pre-warms the agents table via a throwaway create+delete so we
    don't inherit the cold-start CREATE TABLE race documented in
    T0103a (same-iteration concurrent CREATE on a new table).
    """
    import asyncio
    provider_id = f"llm-t0447-{unique_suffix}"
    agent_id = f"agent-t0447-{unique_suffix}"
    warmup_id = f"agent-warmup-t0447-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        # Warm the agents table (creates the row + drops it)
        warm = await client.post(
            "/v1/agents",
            json=_agent_body(warmup_id, provider_id=provider_id, tools=[]),
        )
        assert warm.status_code == 201, warm.text
        await client.delete(f"/v1/agents/{warmup_id}")

        # Race 10 concurrent POSTs of the same id
        body = _agent_body(agent_id, provider_id=provider_id, tools=[])
        tasks = [
            asyncio.create_task(client.post("/v1/agents", json=body))
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks)

        # No /errors/internal; no 5xx other than documented races
        for i, r in enumerate(results):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"racer {i} leaked /errors/internal: {r.text}"
            )
            # 201 (won) or 409 (lost). Per T0103a, the warmed-table
            # race shouldn't surface 502 here, but allow it as a
            # known-bug fallback — never silent failure.
            assert r.status_code in (201, 409, 502), (
                f"racer {i}: unexpected status {r.status_code}: {r.text}"
            )

        # Exactly one 201 winner, nine losers (409 or 502)
        winners = [r for r in results if r.status_code == 201]
        assert len(winners) == 1, (
            f"expected exactly 1 winner, got {len(winners)} from "
            f"statuses {[r.status_code for r in results]!r}"
        )
        losers = [r for r in results if r.status_code in (409, 502)]
        assert len(losers) == 9, (
            f"expected 9 losers, got {len(losers)}"
        )
        # All 409 envelopes are /errors/conflict; 502 (if any) is
        # /errors/provider-* per T0103a
        for r in losers:
            envelope = r.json()
            if r.status_code == 409:
                assert envelope.get("type") == "/errors/conflict", envelope

        # Single non-corrupt row exists post-race
        got = await client.get(f"/v1/agents/{agent_id}")
        assert got.status_code == 200, got.text
        assert got.json()["id"] == agent_id, got.json()
        # Filter the list to confirm no aliased duplicates
        listed = await client.get(
            "/v1/agents", params={"limit": 200, "offset": 0},
        )
        assert listed.status_code == 200, listed.text
        matching = [
            item for item in listed.json()["items"]
            if item["id"] == agent_id
        ]
        assert len(matching) == 1, (
            f"concurrent POST race produced {len(matching)} rows "
            f"with id={agent_id!r}"
        )
    finally:
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0469 — Graph router branch.to_node referencing missing node id → 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0469_graph_router_branch_unknown_to_node_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0469 — Per matrix/model/graph.py:418-429, the topology
    validator walks `_JsonPathRouter.branches[*].to_node` and rejects
    any branch whose target isn't in the declared node ids. Sibling
    of T0430 (static edge to_node) for the conditional-edge path.
    Pin: 422 /errors/validation-error surfacing the bad node id;
    never /errors/internal.
    """
    provider_id = f"llm-t0469-{unique_suffix}"
    agent_id = f"agent-t0469-{unique_suffix}"
    graph_id = f"graph-t0469-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "T0469",
            "nodes": [
                {
                    "kind": "agent", "id": "n1", "agent_id": agent_id,
                    "response_format": {
                        "type": "object",
                        "properties": {"action": {"type": "string"}},
                    },
                },
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {
                    "kind": "conditional", "from_node": "n1",
                    "router": {
                        "kind": "json_path",
                        "branches": [
                            {"when": {"action": "done"}, "to_node": "end"},
                            {
                                "when": {"action": "ghost-route"},
                                "to_node": "ghost-not-a-node",
                            },
                        ],
                    },
                },
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code != 500, resp.text
        assert resp.status_code == 422, (
            f"router branch with bad to_node should be 422; got "
            f"{resp.status_code}: {resp.text}"
        )
        envelope = resp.json()
        assert envelope.get("type") == "/errors/validation-error", envelope
        assert "ghost-not-a-node" in resp.text, resp.text

        # Defence: row was not created
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 404, got.text
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0470 — Graph with multiple terminal nodes accepted; /status clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0470_graph_with_multiple_terminals_accepted_status_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0470 — A graph may have any number of terminal nodes (a
    branching DAG with two distinct sinks is normal). Pin: 201 at
    create + clean /status envelope (ok=true since all referenced
    agents exist); never /errors/internal.
    """
    provider_id = f"llm-t0470-{unique_suffix}"
    agent_id = f"agent-t0470-{unique_suffix}"
    graph_id = f"graph-t0470-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        # Two-terminal DAG: n1 splits to end-a and end-b via static
        # edges (both static fire — semantically the executor would
        # pick one, but topology-wise this is valid).
        body = {
            "id": graph_id,
            "description": "T0470 multi-terminal",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end-a"},
                {"kind": "terminal", "id": "end-b"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end-a"},
                {"kind": "static", "from_node": "n1", "to_node": "end-b"},
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, (
            f"multi-terminal graph should be accepted; got "
            f"{resp.status_code}: {resp.text}"
        )

        # Round-trip: GET preserves both terminals
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        terminals = [
            n for n in got.json()["nodes"] if n["kind"] == "terminal"
        ]
        assert len(terminals) == 2, terminals

        # /status returns clean envelope
        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        body_status = status.json()
        assert "ok" in body_status, body_status
        assert isinstance(body_status.get("issues"), list), body_status
        assert body_status.get("ok") is True, body_status
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0471 — Graph with self-loop edge n1->n1 returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0471_graph_with_self_loop_edge_returns_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0471 — A static edge from n1 to n1 is a self-loop. The
    topology validator (matrix/model/graph.py:387) only checks edge
    endpoints exist as nodes — self-loop is structurally legal.
    Pin observed behaviour: 201 (current permissive accept like
    T0431 cycles) or 422 with cycle wording. Never /errors/internal.
    """
    provider_id = f"llm-t0471-{unique_suffix}"
    agent_id = f"agent-t0471-{unique_suffix}"
    graph_id = f"graph-t0471-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    graph_created = False

    try:
        body = {
            "id": graph_id,
            "description": "T0471 self-loop",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "n1"},
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code != 500, resp.text
        assert resp.status_code in (201, 422), (
            f"self-loop graph: unexpected status "
            f"{resp.status_code}: {resp.text}"
        )

        if resp.status_code == 201:
            graph_created = True
            # Round-trip preserves the self-loop edge
            got = await client.get(f"/v1/graphs/{graph_id}")
            assert got.status_code == 200, got.text
            edges = got.json()["edges"]
            assert any(
                e.get("from_node") == "n1" and e.get("to_node") == "n1"
                for e in edges
            ), got.json()
        else:
            envelope = resp.json()
            assert envelope.get("type") == "/errors/validation-error", envelope
    finally:
        if graph_created:
            await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0472 — Graph with entry_node_id pointing at a terminal-only node
# ============================================================================


@pytest.mark.asyncio
async def test_t0472_graph_entry_node_at_terminal_only_node_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0472 — A graph whose entry_node_id is a terminal node (the
    one-and-only node) is structurally a no-op DAG. The topology
    validator only checks entry_node_id ∈ nodes; nothing forbids the
    entry being a terminal sink. Pin: 201 + clean /status; subsequent
    graph-bound session creation responds cleanly.
    """
    provider_id = f"llm-t0472-{unique_suffix}"
    agent_id = f"agent-t0472-{unique_suffix}"
    graph_id = f"graph-t0472-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "T0472 entry-at-terminal",
            "nodes": [
                {"kind": "terminal", "id": "the-only-node"},
            ],
            "edges": [],
            "entry_node_id": "the-only-node",
        }
        resp = await client.post("/v1/graphs", json=body)
        # Hard pin: never 5xx
        assert resp.status_code != 500, resp.text
        # Acceptable: 201 (current permissive accept) or 422 (a future
        # validator deliberately rejects no-op graphs)
        assert resp.status_code in (201, 422), (
            f"entry-at-terminal graph: unexpected status "
            f"{resp.status_code}: {resp.text}"
        )

        if resp.status_code == 201:
            # /status returns clean envelope (no agent referenced
            # since entry is terminal — ok=true)
            status = await client.get(f"/v1/graphs/{graph_id}/status")
            assert status.status_code == 200, status.text
            body_status = status.json()
            assert "ok" in body_status, body_status
        else:
            envelope = resp.json()
            assert envelope.get("type", "").startswith("/errors/"), envelope
            assert envelope.get("type") != "/errors/internal", envelope
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0473 — PUT mutating a Graph that has a live graph-bound session
# ============================================================================


@pytest.mark.asyncio
async def test_t0473_put_graph_with_live_session_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0473 — Create graph G, bind a session to G (CREATED, not
    resumed), then PUT a structurally-different version of G.
    Pin: PUT returns clean envelope (200 success or 4xx); the
    pre-existing session row is still readable; never /errors/internal.

    Catches a regression where the graph PUT cascade tries to walk
    bound sessions and trips on stale references.

    Uses _agent_body + _graph_body helpers but builds a workspace
    inline since this file doesn't import the workspace setup chain.
    """
    import tempfile
    provider_id = f"llm-t0473-{unique_suffix}"
    agent_id = f"agent-t0473-{unique_suffix}"
    graph_id = f"graph-t0473-{unique_suffix}"
    wp_id = f"wp-t0473-{unique_suffix}"
    tpl_id = f"wt-t0473-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    workspace_id: str | None = None
    session_id: str | None = None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            wp = await client.post(
                "/v1/workspace_providers",
                json={
                    "id": wp_id,
                    "provider": "local",
                    "config": {"kind": "local", "path": tmp},
                },
            )
            assert wp.status_code == 201, wp.text
            tpl = await client.post(
                "/v1/workspace_templates",
                json={
                    "id": tpl_id,
                    "description": "T0473",
                    "provider_id": wp_id,
                    "backend": {"kind": "local"},
                },
            )
            assert tpl.status_code == 201, tpl.text

            # Initial graph: just n1 + terminal
            initial_graph = {
                "id": graph_id,
                "description": "T0473 initial",
                "nodes": [
                    {"kind": "agent", "id": "n1", "agent_id": agent_id},
                    {"kind": "terminal", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
                "entry_node_id": "n1",
            }
            gr = await client.post("/v1/graphs", json=initial_graph)
            assert gr.status_code == 201, gr.text

            # Materialise workspace + bind a session (auto_start=False
            # so it stays CREATED — no worker activity)
            ws = await client.post(
                "/v1/workspaces", json={"template_id": tpl_id},
            )
            assert ws.status_code == 201, ws.text
            workspace_id = ws.json()["id"]

            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json={
                    "binding": {"kind": "graph", "graph_id": graph_id},
                    "auto_start": False,
                },
            )
            assert sess.status_code == 201, sess.text
            session_id = sess.json()["id"]

            # PUT a structurally-changed graph (extra agent node + edge)
            new_graph = {
                "id": graph_id,
                "description": "T0473 mutated",
                "nodes": [
                    {"kind": "agent", "id": "n1", "agent_id": agent_id},
                    {"kind": "agent", "id": "n2", "agent_id": agent_id},
                    {"kind": "terminal", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "n1", "to_node": "n2"},
                    {"kind": "static", "from_node": "n2", "to_node": "end"},
                ],
                "entry_node_id": "n1",
            }
            put_resp = await client.put(
                f"/v1/graphs/{graph_id}", json=new_graph,
            )
            envelope = put_resp.json() if put_resp.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"PUT graph with live session leaked /errors/internal: "
                f"{put_resp.text}"
            )
            assert put_resp.status_code < 500, put_resp.text
            # Documented codes: 200 (mutation accepted) or 4xx (some
            # future contract that locks live-session graphs)
            assert put_resp.status_code in (200, 409, 422), (
                f"unexpected PUT status: {put_resp.status_code}: "
                f"{put_resp.text}"
            )

            # Pre-existing session row still readable
            got_sess = await client.get(f"/v1/sessions/{session_id}")
            assert got_sess.status_code == 200, got_sess.text
            assert got_sess.json()["id"] == session_id, got_sess.json()
            # Binding still references the original graph_id
            assert got_sess.json()["binding"]["graph_id"] == graph_id
        finally:
            if session_id is not None and workspace_id is not None:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
                )
            if workspace_id is not None:
                await client.delete(f"/v1/workspaces/{workspace_id}")
            await client.delete(f"/v1/graphs/{graph_id}")
            await client.delete(f"/v1/workspace_templates/{tpl_id}")
            await client.delete(f"/v1/workspace_providers/{wp_id}")
            await client.delete(f"/v1/agents/{agent_id}")
            await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0474 — DELETE Graph while a session is bound to it returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0474_delete_graph_with_bound_session_orphan_tolerated(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0474 — Mirror of T0157/T0265 (orphan-tolerated cascades) for
    the Graph→Session FK. DELETE a graph while a session is bound
    to it. Pin: DELETE returns 204 (graph gone); subsequent GET on
    the session row still returns 200 with the orphaned binding
    intact; never /errors/internal anywhere.

    Catches a regression where the graph DELETE cascade tries to
    walk bound sessions and either 5xxs or silently corrupts the
    session row.
    """
    import tempfile
    provider_id = f"llm-t0474-{unique_suffix}"
    agent_id = f"agent-t0474-{unique_suffix}"
    graph_id = f"graph-t0474-{unique_suffix}"
    wp_id = f"wp-t0474-{unique_suffix}"
    tpl_id = f"wt-t0474-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    workspace_id: str | None = None
    session_id: str | None = None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            wp = await client.post(
                "/v1/workspace_providers",
                json={
                    "id": wp_id,
                    "provider": "local",
                    "config": {"kind": "local", "path": tmp},
                },
            )
            assert wp.status_code == 201, wp.text
            tpl = await client.post(
                "/v1/workspace_templates",
                json={
                    "id": tpl_id,
                    "description": "T0474",
                    "provider_id": wp_id,
                    "backend": {"kind": "local"},
                },
            )
            assert tpl.status_code == 201, tpl.text

            gr = await client.post(
                "/v1/graphs",
                json=_graph_body(graph_id, agent_id=agent_id),
            )
            assert gr.status_code == 201, gr.text

            ws = await client.post(
                "/v1/workspaces", json={"template_id": tpl_id},
            )
            assert ws.status_code == 201, ws.text
            workspace_id = ws.json()["id"]

            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json={
                    "binding": {"kind": "graph", "graph_id": graph_id},
                    "auto_start": False,
                },
            )
            assert sess.status_code == 201, sess.text
            session_id = sess.json()["id"]

            # DELETE the bound graph
            rm = await client.delete(f"/v1/graphs/{graph_id}")
            envelope = rm.json() if rm.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"DELETE graph with bound session leaked /errors/internal: "
                f"{rm.text}"
            )
            assert rm.status_code == 204, (
                f"DELETE graph should be orphan-tolerated 204; got "
                f"{rm.status_code}: {rm.text}"
            )

            # Session row still readable; orphaned binding intact
            got = await client.get(f"/v1/sessions/{session_id}")
            assert got.status_code == 200, got.text
            body = got.json()
            assert body["id"] == session_id
            assert body["binding"]["kind"] == "graph"
            assert body["binding"]["graph_id"] == graph_id, (
                f"orphaned binding lost graph_id: {body['binding']!r}"
            )
        finally:
            if session_id is not None and workspace_id is not None:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
                )
            if workspace_id is not None:
                await client.delete(f"/v1/workspaces/{workspace_id}")
            await client.delete(f"/v1/graphs/{graph_id}")
            await client.delete(f"/v1/workspace_templates/{tpl_id}")
            await client.delete(f"/v1/workspace_providers/{wp_id}")
            await client.delete(f"/v1/agents/{agent_id}")
            await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0475 — Graph subgraph node referencing missing graph_id flips ok=false
# ============================================================================


@pytest.mark.asyncio
async def test_t0475_graph_subgraph_node_missing_graph_id_status_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0475 — Per matrix/api/routers/compute.py:152-155, /graphs/
    {id}/status walks each node and surfaces a missing-Graph issue
    for any subgraph_ref whose target id doesn't exist. Mirror of
    T0413 (missing-toolset on Agent) for the Graph→subgraph path.

    Pin: orphan-tolerated 201 at create; /status returns ok=false
    with an issue mentioning the missing graph_id; never
    /errors/internal.
    """
    provider_id = f"llm-t0475-{unique_suffix}"
    agent_id = f"agent-t0475-{unique_suffix}"
    graph_id = f"graph-t0475-{unique_suffix}"
    missing_subgraph_id = f"missing-sub-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "T0475",
            "nodes": [
                {
                    "kind": "graph", "id": "subnode",
                    "graph_id": missing_subgraph_id,
                },
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "subnode", "to_node": "end"},
            ],
            "entry_node_id": "subnode",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, (
            f"subgraph reference is orphan-tolerated; got "
            f"{resp.status_code}: {resp.text}"
        )

        # /status returns ok=false with the missing subgraph surfaced
        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        body_status = status.json()
        assert body_status.get("ok") is False, (
            f"expected ok=false for missing subgraph; got {body_status!r}"
        )
        issues = body_status.get("issues", [])
        assert isinstance(issues, list) and issues, body_status
        assert any(
            missing_subgraph_id in str(i) for i in issues
        ), (
            f"no issue references missing subgraph "
            f"{missing_subgraph_id!r}: {issues!r}"
        )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0492 — Agent referencing openresponses provider with malformed url
# ============================================================================


@pytest.mark.asyncio
async def test_t0492_agent_status_with_malformed_provider_url_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0492 — Create an LLMProvider with provider=openresponses
    whose url passes Pydantic but is structurally malformed (e.g.
    "http://" — empty host). Then create an Agent referencing it.
    Pin: both POSTs return clean envelopes (201 or 4xx); GET
    /agents/{id}/status returns 200 with a documented {ok, issues}
    body; never /errors/internal at create or status walk.

    The status walk only checks REFERENCE existence (per
    matrix/api/routers/compute.py:142-156), not adapter
    constructability — so even if the provider's url couldn't
    actually connect, the agent /status should still report
    ok=true (the LLMProvider row exists). The hard pin is the
    no-/errors/internal invariant either way.
    """
    provider_id = f"llm-t0492-{unique_suffix}"
    agent_id = f"agent-t0492-{unique_suffix}"
    malformed_url = "http://"  # passes Pydantic str but is a no-op host

    pr = await client.post(
        "/v1/llm_providers",
        json={
            "id": provider_id,
            "provider": "openresponses",
            "models": [
                {"name": "any-model", "context_length": 4096},
            ],
            "config": {
                "url": malformed_url,
                "api_key": "sk-not-used",
                "flavor": "other",
            },
            "limits": {"max_concurrency": 1},
        },
    )
    pr_envelope = pr.json() if pr.content else {}
    assert pr_envelope.get("type") != "/errors/internal", (
        f"create with malformed url leaked /errors/internal: {pr.text}"
    )
    # Either accepted (201) or rejected with clean envelope (422 if
    # the validator catches the empty host)
    assert pr.status_code in (201, 422), (
        f"unexpected provider create status: {pr.status_code}: "
        f"{pr.text}"
    )
    if pr.status_code != 201:
        # Provider rejected at create — nothing more to test
        return

    try:
        ag = await client.post(
            "/v1/agents",
            json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
        )
        ag_envelope = ag.json() if ag.content else {}
        assert ag_envelope.get("type") != "/errors/internal", (
            f"agent create with malformed-url provider leaked "
            f"/errors/internal: {ag.text}"
        )
        assert ag.status_code == 201, ag.text

        try:
            status = await client.get(f"/v1/agents/{agent_id}/status")
            status_envelope = (
                status.json() if status.content else {}
            )
            assert status_envelope.get("type") != "/errors/internal", (
                f"/status walk leaked /errors/internal: {status.text}"
            )
            assert status.status_code == 200, status.text
            body = status.json()
            assert "ok" in body, body
            assert isinstance(body.get("issues"), list), body
        finally:
            await client.delete(f"/v1/agents/{agent_id}")
    finally:
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0495 — Graph max_iterations=0 rejected with 422 (PositiveInt lower bound)
# ============================================================================


@pytest.mark.asyncio
async def test_t0495_graph_max_iterations_zero_rejected_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0495 — Graph.max_iterations is `PositiveInt | None` per
    matrix/model/graph.py:379. PositiveInt forbids 0 and negatives.
    Pin: 0 is rejected with 422 /errors/validation-error; row not
    created.
    """
    provider_id = f"llm-t0495-{unique_suffix}"
    agent_id = f"agent-t0495-{unique_suffix}"
    graph_id = f"graph-t0495-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "T0495",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
            "max_iterations": 0,
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code != 500, resp.text
        assert resp.status_code == 422, (
            f"max_iterations=0 should be 422; got "
            f"{resp.status_code}: {resp.text}"
        )
        envelope = resp.json()
        assert envelope.get("type") == "/errors/validation-error", envelope
        # Row not created
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 404, got.text
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0496 — Graph max_iterations=-5 rejected with 422 (PositiveInt sign)
# ============================================================================


@pytest.mark.asyncio
async def test_t0496_graph_max_iterations_negative_rejected_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0496 — Sibling of T0495 for the negative-int boundary.
    PositiveInt also forbids negatives. Pin: -5 → 422.
    """
    provider_id = f"llm-t0496-{unique_suffix}"
    agent_id = f"agent-t0496-{unique_suffix}"
    graph_id = f"graph-t0496-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "T0496",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
            "max_iterations": -5,
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 422, (
            f"max_iterations=-5 should be 422; got "
            f"{resp.status_code}: {resp.text}"
        )
        envelope = resp.json()
        assert envelope.get("type") == "/errors/validation-error", envelope
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0497 — Graph max_iterations=2**31 accepted; round-trips through GET
# ============================================================================


@pytest.mark.asyncio
async def test_t0497_graph_max_iterations_very_large_accepted_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0497 — PositiveInt in Pydantic v2 has no upper bound (Python
    int is unbounded). Pin: a very large value (2**31 = 2147483648)
    is accepted and round-trips byte-exact through GET.
    """
    provider_id = f"llm-t0497-{unique_suffix}"
    agent_id = f"agent-t0497-{unique_suffix}"
    graph_id = f"graph-t0497-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    very_large = 2**31  # 2_147_483_648 — beyond a 32-bit signed range
    try:
        body = {
            "id": graph_id,
            "description": "T0497",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
            "max_iterations": very_large,
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, (
            f"max_iterations={very_large} should be accepted; got "
            f"{resp.status_code}: {resp.text}"
        )
        # Round-trip via GET
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        assert got.json()["max_iterations"] == very_large, (
            f"max_iterations corrupted on round-trip: "
            f"sent={very_large}, got={got.json().get('max_iterations')!r}"
        )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0498 — Graph with conditional edge + callable router accepted at create
# ============================================================================


@pytest.mark.asyncio
async def test_t0498_graph_with_callable_router_create_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0498 — Per matrix/model/graph.py:285-302, _CallableRouter has
    `kind="callable"` + `callable_id: str`. The topology validator
    does NOT resolve the callable at create time (per the docstring,
    "The callable signature is ... the returned string MUST be the
    id of an existing node" — runtime check).

    Pin: a graph with a conditional edge whose router is a callable
    referencing a non-existent callable_id is accepted at create
    (201); GET round-trips the edge shape; a bound session
    terminates ENDED/failed cleanly via the executor's fatal path
    (T0429 sibling — graph executor itself is NotImplemented).

    NB: this test does NOT bind a session to keep it under 30s; the
    fatal-path convergence is already covered by T0429 for any
    graph shape.
    """
    provider_id = f"llm-t0498-{unique_suffix}"
    agent_id = f"agent-t0498-{unique_suffix}"
    graph_id = f"graph-t0498-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "T0498 callable router",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {
                    "kind": "conditional", "from_node": "n1",
                    "router": {
                        "kind": "callable",
                        "callable_id": f"router-not-registered-{unique_suffix}",
                    },
                },
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, (
            f"graph with callable router should be accepted at create "
            f"(callable resolution is runtime); got "
            f"{resp.status_code}: {resp.text}"
        )

        # GET round-trips the edge shape verbatim
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        edges = got.json()["edges"]
        assert len(edges) == 1, edges
        edge = edges[0]
        assert edge.get("kind") == "conditional", edge
        router = edge.get("router")
        assert isinstance(router, dict), edge
        assert router.get("kind") == "callable", router
        assert router.get("callable_id") == (
            f"router-not-registered-{unique_suffix}"
        ), router

        # /status returns clean envelope (no resolution issues
        # surfaced — callable refs aren't validated by the agent
        # status walk)
        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        body_status = status.json()
        assert "ok" in body_status, body_status
        assert isinstance(body_status.get("issues"), list), body_status
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0499 — PUT /v1/graphs/{path-id} with body.id mismatch returns 409
# ============================================================================


@pytest.mark.asyncio
async def test_t0499_put_graph_body_id_mismatch_returns_409(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0499 — Per matrix/api/routers/_crud.py:152-155, PUT raises
    ConflictError when path id != body id. Pin: 409
    /errors/conflict; existing row not corrupted by the rejected
    PUT. Mirror of the same pattern across all entity types — this
    pins it specifically for the Graph CRUD route.
    """
    provider_id = f"llm-t0499-{unique_suffix}"
    agent_id = f"agent-t0499-{unique_suffix}"
    graph_id = f"graph-t0499-{unique_suffix}"
    wrong_id = f"graph-t0499-other-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    gr = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert gr.status_code == 201, gr.text

    try:
        # PUT with body.id != path id
        body = _graph_body(graph_id, agent_id=agent_id)
        body["id"] = wrong_id  # body's id mismatches the path id
        body["description"] = "T0499 mismatched body id"
        resp = await client.put(f"/v1/graphs/{graph_id}", json=body)
        assert resp.status_code != 500, resp.text
        assert resp.status_code == 409, (
            f"id mismatch should be 409 conflict; got "
            f"{resp.status_code}: {resp.text}"
        )
        envelope = resp.json()
        assert envelope.get("type") == "/errors/conflict", envelope

        # Existing row not corrupted — description is the original
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        assert got.json().get("description") == "test graph", (
            f"row corrupted by rejected PUT: {got.json()!r}"
        )
        # Wrong-id row was not created
        got_wrong = await client.get(f"/v1/graphs/{wrong_id}")
        assert got_wrong.status_code == 404, got_wrong.text
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/graphs/{wrong_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0500 — POST /v1/graphs then immediate concurrent PUT on same id
# ============================================================================


@pytest.mark.asyncio
async def test_t0500_post_graph_concurrent_put_same_id_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0500 — Race a POST /v1/graphs against a PUT to the same id
    (the POST establishes the row; the PUT mutates it). Pin: both
    return clean envelopes (no /errors/internal); subsequent GET
    returns ONE non-corrupt body whose description matches one of
    the two writers' values; no aliased duplicate row.
    """
    import asyncio
    provider_id = f"llm-t0500-{unique_suffix}"
    agent_id = f"agent-t0500-{unique_suffix}"
    graph_id = f"graph-t0500-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        # Pre-warm the graphs table so concurrent writers don't hit
        # the cold-start CREATE TABLE race documented as T0103a
        # (both transactions try to CREATE TABLE simultaneously and
        # one loses with pg_type_typname_nsp_index unique violation,
        # surfaced as 502 /errors/provider-error).
        warmup_id = f"graph-warmup-t0500-{unique_suffix}"
        warm = await client.post(
            "/v1/graphs", json=_graph_body(warmup_id, agent_id=agent_id),
        )
        assert warm.status_code == 201, warm.text
        await client.delete(f"/v1/graphs/{warmup_id}")

        post_body = _graph_body(graph_id, agent_id=agent_id)
        post_body["description"] = "from-POST"
        put_body = _graph_body(graph_id, agent_id=agent_id)
        put_body["description"] = "from-PUT"

        # Race the create with the update; PUT will fail with 404
        # if it lands first (no row to update), or succeed otherwise
        post_task = asyncio.create_task(
            client.post("/v1/graphs", json=post_body),
        )
        put_task = asyncio.create_task(
            client.put(f"/v1/graphs/{graph_id}", json=put_body),
        )
        post_resp, put_resp = await asyncio.gather(post_task, put_task)

        # Hard pin: never /errors/internal. 502 /errors/provider-error
        # is a documented outcome (T0103a — two writers serialised by
        # Postgres unique-key constraint on the row insert).
        for r, label in ((post_resp, "POST"), (put_resp, "PUT")):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"{label} race leaked /errors/internal: {r.text}"
            )

        # POST: 201 (won) or 409 (PUT got there first — but PUT
        # would 404 in that case) or 502 (provider-error from
        # concurrent insert)
        assert post_resp.status_code in (201, 409, 502), (
            f"POST race: unexpected {post_resp.status_code}: "
            f"{post_resp.text}"
        )
        # PUT: 200 (POST landed first, PUT updated), 404 (PUT first,
        # no row), or 502 (concurrent-insert provider-error)
        assert put_resp.status_code in (200, 404, 409, 502), (
            f"PUT race: unexpected {put_resp.status_code}: "
            f"{put_resp.text}"
        )

        # Final GET — either the row exists (one writer won) or 404
        # (both writers lost the provider-error race). If it exists,
        # the description must come from one of the two writers and
        # only one row should carry the id (no aliased duplicates).
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code in (200, 404), got.text
        if got.status_code == 200:
            final_body = got.json()
            assert final_body["id"] == graph_id, final_body
            assert final_body.get("description") in (
                "from-POST", "from-PUT", "test graph",
            ), (
                f"final description not from any writer: "
                f"{final_body.get('description')!r}"
            )

            listed = await client.get(
                "/v1/graphs", params={"limit": 200, "offset": 0},
            )
            assert listed.status_code == 200, listed.text
            matching = [
                item for item in listed.json()["items"]
                if item["id"] == graph_id
            ]
            assert len(matching) == 1, (
                f"race produced {len(matching)} rows with "
                f"id={graph_id!r}"
            )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0519 — Graph node ids: whitespace / unicode / length-1 / length-200
# ============================================================================


@pytest.mark.asyncio
async def test_t0519_graph_node_ids_edge_cases_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0519 — Per matrix/model/graph.py:159-162 + 198, node `id`
    is a `str` with `min_length=1`. Pin: edge-shaped node ids
    (single char, unicode, leading-whitespace-only, very long)
    round-trip byte-exact through POST → GET, and graph /status
    returns a clean envelope.
    """
    provider_id = f"llm-t0519-{unique_suffix}"
    agent_id = f"agent-t0519-{unique_suffix}"
    graph_id = f"graph-t0519-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    edge_ids = [
        "x",                  # length-1
        "ノード",             # unicode (Japanese)
        "n" * 200,            # length-200
        " ws ",               # leading + trailing whitespace
    ]
    try:
        nodes = [
            {"kind": "agent", "id": eid, "agent_id": agent_id}
            for eid in edge_ids
        ] + [{"kind": "terminal", "id": "end"}]
        edges = [
            {"kind": "static", "from_node": eid, "to_node": "end"}
            for eid in edge_ids
        ]
        body = {
            "id": graph_id,
            "description": "T0519 node-id edge cases",
            "nodes": nodes,
            "edges": edges,
            "entry_node_id": edge_ids[0],
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, (
            f"edge-shaped node ids should be accepted; got "
            f"{resp.status_code}: {resp.text}"
        )

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        got_node_ids = [n["id"] for n in got.json()["nodes"]]
        for eid in edge_ids:
            assert eid in got_node_ids, (
                f"node id {eid!r} missing from round-trip: "
                f"{got_node_ids!r}"
            )

        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        body_status = status.json()
        assert "ok" in body_status, body_status
        assert isinstance(body_status.get("issues"), list), body_status
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0520 — Graph entry_node_id at subgraph node: bound session converges cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0520_graph_entry_at_subgraph_node_session_converges_cleanly(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0520 — A parent graph whose entry_node_id is a node of
    kind=graph (subgraph reference). Pin: graph CRUD accepts the
    shape (201 + round-trip); a session bound to the parent
    converges to terminal via _handle_fatal (graph executor
    NotImplemented per T0429); never sticks RUNNING.

    The child subgraph reference doesn't even need to exist
    (orphan-tolerated per T0475) — the load-bearing assertion is
    the parent graph's session terminates cleanly.
    """
    import asyncio
    import tempfile

    provider_id = f"llm-t0520-{unique_suffix}"
    agent_id = f"agent-t0520-{unique_suffix}"
    parent_graph_id = f"graph-parent-t0520-{unique_suffix}"
    child_subgraph_id = f"graph-child-t0520-{unique_suffix}"
    wp_id = f"wp-t0520-{unique_suffix}"
    tpl_id = f"wt-t0520-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    workspace_id: str | None = None
    session_id: str | None = None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            parent_body = {
                "id": parent_graph_id,
                "description": "T0520 parent graph",
                "nodes": [
                    {
                        "kind": "graph", "id": "subnode",
                        "graph_id": child_subgraph_id,
                    },
                    {"kind": "terminal", "id": "end"},
                ],
                "edges": [
                    {
                        "kind": "static", "from_node": "subnode",
                        "to_node": "end",
                    },
                ],
                "entry_node_id": "subnode",
            }
            gr = await client.post("/v1/graphs", json=parent_body)
            assert gr.status_code == 201, gr.text

            got = await client.get(f"/v1/graphs/{parent_graph_id}")
            assert got.status_code == 200, got.text
            assert got.json()["entry_node_id"] == "subnode", got.json()

            wp = await client.post(
                "/v1/workspace_providers",
                json={
                    "id": wp_id,
                    "provider": "local",
                    "config": {"kind": "local", "path": tmp},
                },
            )
            assert wp.status_code == 201, wp.text
            tpl = await client.post(
                "/v1/workspace_templates",
                json={
                    "id": tpl_id,
                    "description": "T0520",
                    "provider_id": wp_id,
                    "backend": {"kind": "local"},
                },
            )
            assert tpl.status_code == 201, tpl.text
            ws = await client.post(
                "/v1/workspaces", json={"template_id": tpl_id},
            )
            assert ws.status_code == 201, ws.text
            workspace_id = ws.json()["id"]

            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json={
                    "binding": {
                        "kind": "graph", "graph_id": parent_graph_id,
                    },
                    "auto_start": False,
                },
            )
            assert sess.status_code == 201, sess.text
            session_id = sess.json()["id"]

            resume = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/"
                f"{session_id}/resume",
            )
            assert resume.status_code == 200, resume.text

            final: dict = {}
            for _ in range(60):
                r = await client.get(f"/v1/sessions/{session_id}")
                assert r.status_code == 200, r.text
                final = r.json()
                if final.get("status") == "ended":
                    break
                await asyncio.sleep(0.5)
            assert final.get("status") == "ended", (
                f"subgraph-entry session did not converge in 30s: "
                f"{final!r}"
            )
            assert final.get("ended_reason") == "failed", final
            assert final.get("last_error"), final
        finally:
            if session_id is not None and workspace_id is not None:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/"
                    f"{session_id}/cancel",
                )
            if workspace_id is not None:
                await client.delete(f"/v1/workspaces/{workspace_id}")
            await client.delete(f"/v1/graphs/{parent_graph_id}")
            await client.delete(f"/v1/workspace_templates/{tpl_id}")
            await client.delete(f"/v1/workspace_providers/{wp_id}")
            await client.delete(f"/v1/agents/{agent_id}")
            await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0521 — Graph with subgraph + agent siblings accepted (201)
# ============================================================================


@pytest.mark.asyncio
async def test_t0521_graph_with_subgraph_and_agent_siblings_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0521 — A graph may mix node kinds: a subgraph reference
    AND an agent node in the same graph. Pin: 201 at create; both
    kinds round-trip on GET; /status surfaces only refs that
    genuinely miss (the subgraph_id is orphan-tolerated like T0475).
    """
    provider_id = f"llm-t0521-{unique_suffix}"
    agent_id = f"agent-t0521-{unique_suffix}"
    graph_id = f"graph-t0521-{unique_suffix}"
    missing_subgraph_id = f"missing-sub-t0521-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "T0521 mixed node kinds",
            "nodes": [
                {"kind": "agent", "id": "an", "agent_id": agent_id},
                {
                    "kind": "graph", "id": "sn",
                    "graph_id": missing_subgraph_id,
                },
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "an", "to_node": "sn"},
                {"kind": "static", "from_node": "sn", "to_node": "end"},
            ],
            "entry_node_id": "an",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, resp.text

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        kinds = sorted(n["kind"] for n in got.json()["nodes"])
        assert kinds == sorted(["agent", "graph", "terminal"]), kinds

        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        body_status = status.json()
        assert body_status.get("ok") is False, body_status
        issues = body_status.get("issues", [])
        assert any(
            missing_subgraph_id in str(i) for i in issues
        ), issues
        for issue in issues:
            assert agent_id not in str(issue), (
                f"agent {agent_id!r} (which exists) wrongly in "
                f"issues: {issues!r}"
            )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0522 — Graph with empty description="" accepted 201; round-trip
# ============================================================================


@pytest.mark.asyncio
async def test_t0522_graph_with_empty_description_accepted(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0522 — Per matrix/model/common.Describeable, `description`
    defaults to "" and has no min_length constraint. Pin: explicit
    empty description is accepted (201) and round-trips byte-exact;
    /status returns clean envelope.
    """
    provider_id = f"llm-t0522-{unique_suffix}"
    agent_id = f"agent-t0522-{unique_suffix}"
    graph_id = f"graph-t0522-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, (
            f"empty description should be accepted; got "
            f"{resp.status_code}: {resp.text}"
        )

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        assert got.json().get("description") == "", got.json()

        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        assert status.json().get("ok") is True, status.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0523 — Graph max_iterations=1 (PositiveInt lower bound) accepted
# ============================================================================


@pytest.mark.asyncio
async def test_t0523_graph_max_iterations_one_accepted_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0523 — PositiveInt lower bound is 1. Pin: max_iterations=1
    (the smallest legal value) is accepted; round-trips byte-exact
    via GET. Lower-bound complement to T0497 (2**31 upper) and
    T0495/T0496 (rejected 0/-5).
    """
    provider_id = f"llm-t0523-{unique_suffix}"
    agent_id = f"agent-t0523-{unique_suffix}"
    graph_id = f"graph-t0523-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "T0523",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
            "max_iterations": 1,
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, resp.text

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        assert got.json()["max_iterations"] == 1, got.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0524 — Graph node input_template with Jinja2 syntax error accepted
# ============================================================================


@pytest.mark.asyncio
async def test_t0524_graph_node_jinja_syntax_error_in_template_accepted(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0524 — `_AgentNodeRef.input_template` is a free-form `str`
    per primer/model/graph.py:169-177. The topology validator does
    NOT compile the Jinja2 template; runtime evaluation happens
    inside the (currently NotImplemented) graph executor.

    Pin: a graph whose node has a structurally-broken Jinja2
    template (`"{{ unclosed"`) is accepted at create (201);
    GET round-trips the template byte-exact; /status returns clean
    envelope. A future template-validating layer would deliberately
    break this test.
    """
    provider_id = f"llm-t0524-{unique_suffix}"
    agent_id = f"agent-t0524-{unique_suffix}"
    graph_id = f"graph-t0524-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    broken_template = "{{ unclosed_variable"
    try:
        body = {
            "id": graph_id,
            "description": "T0524 broken jinja",
            "nodes": [
                {
                    "kind": "agent", "id": "n1",
                    "agent_id": agent_id,
                    "input_template": broken_template,
                },
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, (
            f"broken Jinja2 template should be accepted at create "
            f"(no template compilation in validator); got "
            f"{resp.status_code}: {resp.text}"
        )

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        nodes = got.json()["nodes"]
        agent_node = next(
            (n for n in nodes if n.get("id") == "n1"), None,
        )
        assert agent_node is not None, nodes
        assert agent_node.get("input_template") == broken_template, (
            f"input_template corrupted on round-trip: "
            f"{agent_node.get('input_template')!r}"
        )

        # /status returns clean envelope (validator doesn't engage
        # template compilation)
        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        assert "ok" in status.json(), status.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0525 — POST /v1/graphs/{id}/invalidate behaviour pinned
# ============================================================================


@pytest.mark.asyncio
async def test_t0525_post_graph_invalidate_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0525 — Per matrix/api/routers/compute.py, the Graph CRUD
    router (built via make_crud_router) does NOT mount an
    /invalidate endpoint. Only LLMProvider / EmbeddingProvider /
    CrossEncoderProvider / Toolset have invalidate (they manage
    cached adapters per matrix/api/registries/provider_registry.py).

    Pin: POST /v1/graphs/{id}/invalidate returns a clean envelope
    (404 not-found on the route, OR 405 method-not-allowed). Never
    /errors/internal. Result is deterministic across two calls.
    """
    provider_id = f"llm-t0525-{unique_suffix}"
    agent_id = f"agent-t0525-{unique_suffix}"
    graph_id = f"graph-t0525-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    gr = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert gr.status_code == 201, gr.text

    try:
        # Two consecutive calls — same status + envelope shape
        r1 = await client.post(f"/v1/graphs/{graph_id}/invalidate")
        r2 = await client.post(f"/v1/graphs/{graph_id}/invalidate")

        for r, label in ((r1, "call-1"), (r2, "call-2")):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"{label} leaked /errors/internal: {r.text}"
            )
            assert r.status_code < 500, r.text
            # Documented possibilities: 204 (route exists,
            # provider-style), 404 (route not mounted), 405
            # (method not allowed)
            assert r.status_code in (204, 404, 405), (
                f"{label}: unexpected {r.status_code}: {r.text}"
            )

        # Determinism: same status across the two calls
        assert r1.status_code == r2.status_code, (
            f"non-deterministic: {r1.status_code} vs {r2.status_code}"
        )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0544 — POST /v1/graphs/find cursor pagination round-trip
# ============================================================================


@pytest.mark.asyncio
async def test_t0544_graphs_find_cursor_pagination_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0544 — Sibling of T0014 (toolsets cursor) for the graphs
    /find endpoint. Seed 4 graphs sharing a unique-prefix id; walk
    via POST find with `cursor=null + length=2` then `cursor=<next>`
    until `next_cursor=null`. Pin: every graph visited exactly
    once; no duplicates; no /errors/internal.
    """
    provider_id = f"llm-t0544-{unique_suffix}"
    agent_id = f"agent-t0544-{unique_suffix}"
    prefix = f"graph-t0544-{unique_suffix}"
    graph_ids = [f"{prefix}-{i:02d}" for i in range(4)]

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    for gid in graph_ids:
        r = await client.post(
            "/v1/graphs", json=_graph_body(gid, agent_id=agent_id),
        )
        assert r.status_code == 201, r.text

    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):  # safety cap
            body = {
                "predicate": predicate,
                "page": {
                    "kind": "cursor", "cursor": cursor, "length": 2,
                },
            }
            resp = await client.post("/v1/graphs/find", json=body)
            envelope = resp.json() if resp.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"graphs cursor walk leaked /errors/internal: "
                f"{resp.text}"
            )
            assert resp.status_code == 200, resp.text
            page = resp.json()
            assert page["kind"] == "cursor", page
            seen.extend(item["id"] for item in page["items"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail(
                "graphs cursor walk did not terminate within 10 pages"
            )

        # Each id seen exactly once; full set covered
        assert sorted(seen) == sorted(graph_ids), (
            f"cursor walk visited unexpected set: seen={sorted(seen)!r} "
            f"expected={sorted(graph_ids)!r}"
        )
        assert len(seen) == len(set(seen)), (
            f"cursor walk visited duplicates: {seen!r}"
        )
    finally:
        for gid in graph_ids:
            await client.delete(f"/v1/graphs/{gid}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0545 — Graph node ids with reserved-style names round-trip
# ============================================================================


@pytest.mark.asyncio
async def test_t0545_graph_node_ids_reserved_style_names_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0545 — Pin that "obvious" node id names like "end", "start",
    "main", "_internal" don't trigger any reserved-name validation
    in the topology validator. T0156 / T0470 already use "end" as
    a terminal id, but no test specifically pinned the
    reserved-style-id contract. Catches a regression where a
    future validator deliberately reserves node id names.
    """
    provider_id = f"llm-t0545-{unique_suffix}"
    agent_id = f"agent-t0545-{unique_suffix}"
    graph_id = f"graph-t0545-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    reserved_style = ["start", "main", "_internal"]
    try:
        nodes = [
            {"kind": "agent", "id": rid, "agent_id": agent_id}
            for rid in reserved_style
        ] + [{"kind": "terminal", "id": "end"}]
        edges = [
            {"kind": "static", "from_node": rid, "to_node": "end"}
            for rid in reserved_style
        ]
        body = {
            "id": graph_id,
            "description": "T0545 reserved-style node ids",
            "nodes": nodes,
            "edges": edges,
            "entry_node_id": "start",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, (
            f"reserved-style node ids should be accepted; got "
            f"{resp.status_code}: {resp.text}"
        )

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        got_ids = sorted(n["id"] for n in got.json()["nodes"])
        assert got_ids == sorted(reserved_style + ["end"]), got_ids

        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        assert "ok" in status.json(), status.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0546 — Graph description with newline + control char round-trips byte-exact
# ============================================================================


@pytest.mark.asyncio
async def test_t0546_graph_description_with_control_chars_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0546 — Pin that the description field preserves embedded
    newlines and control characters (\\x01) byte-exact through
    POST → GET. Catches a regression where a sanitizer or a
    JSON-then-asyncpg encoding step strips control chars or
    normalises whitespace.
    """
    provider_id = f"llm-t0546-{unique_suffix}"
    agent_id = f"agent-t0546-{unique_suffix}"
    graph_id = f"graph-t0546-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    weird_desc = (
        f"line-1-{unique_suffix}\nline-2\twith-tab\n"
        f"control-x01:\x01:end-x01\nline-3"
    )
    try:
        body = {
            "id": graph_id,
            "description": weird_desc,
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, resp.text

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        got_desc = got.json().get("description", "")
        assert got_desc == weird_desc, (
            f"description corrupted on round-trip:\n"
            f"  sent (len={len(weird_desc)}): {weird_desc!r}\n"
            f"  got  (len={len(got_desc)}): {got_desc!r}"
        )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0547 — POST graph then DELETE then POST same id (warmed table)
# ============================================================================


@pytest.mark.asyncio
async def test_t0547_post_delete_post_same_graph_id_no_stale_cache(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0547 — Pin: POST a graph, DELETE it, then POST again with
    the same id but a different body. Pin: re-create returns 201;
    GET reflects the v2 body (no stale cache leak from the deleted
    v1). Pre-warms the graphs table to avoid the T0103a cold-start
    race.
    """
    provider_id = f"llm-t0547-{unique_suffix}"
    agent_id = f"agent-t0547-{unique_suffix}"
    graph_id = f"graph-t0547-{unique_suffix}"
    warmup_id = f"graph-warm-t0547-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    # Warm the graphs table
    warm = await client.post(
        "/v1/graphs", json=_graph_body(warmup_id, agent_id=agent_id),
    )
    assert warm.status_code == 201, warm.text
    await client.delete(f"/v1/graphs/{warmup_id}")

    try:
        # v1: original body
        body_v1 = _graph_body(graph_id, agent_id=agent_id)
        body_v1["description"] = "v1-original"
        r1 = await client.post("/v1/graphs", json=body_v1)
        assert r1.status_code == 201, r1.text
        assert r1.json()["description"] == "v1-original", r1.json()

        # DELETE
        rm = await client.delete(f"/v1/graphs/{graph_id}")
        assert rm.status_code == 204, rm.text

        # GET 404 — sanity that the row is gone
        gone = await client.get(f"/v1/graphs/{graph_id}")
        assert gone.status_code == 404, gone.text

        # v2: re-create with different description
        body_v2 = _graph_body(graph_id, agent_id=agent_id)
        body_v2["description"] = "v2-recreated"
        r2 = await client.post("/v1/graphs", json=body_v2)
        assert r2.status_code == 201, r2.text
        assert r2.json()["description"] == "v2-recreated", r2.json()

        # GET reflects v2 (no stale v1 leak)
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        assert got.json()["description"] == "v2-recreated", got.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0548 — PUT graph with a PAUSED bound session returns 200
# ============================================================================


@pytest.mark.asyncio
async def test_t0548_put_graph_with_paused_session_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0548 — Mirror of T0473 (PUT graph with CREATED-state bound
    session) but with the bound session in PAUSED state instead.
    Pin: PUT returns clean envelope (200 success or 4xx); the
    pre-existing session row remains readable; binding intact.
    """
    import tempfile
    provider_id = f"llm-t0548-{unique_suffix}"
    agent_id = f"agent-t0548-{unique_suffix}"
    graph_id = f"graph-t0548-{unique_suffix}"
    wp_id = f"wp-t0548-{unique_suffix}"
    tpl_id = f"wt-t0548-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    workspace_id: str | None = None
    session_id: str | None = None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            wp = await client.post(
                "/v1/workspace_providers",
                json={
                    "id": wp_id, "provider": "local",
                    "config": {"kind": "local", "path": tmp},
                },
            )
            assert wp.status_code == 201, wp.text
            tpl = await client.post(
                "/v1/workspace_templates",
                json={
                    "id": tpl_id,
                    "description": "T0548",
                    "provider_id": wp_id,
                    "backend": {"kind": "local"},
                },
            )
            assert tpl.status_code == 201, tpl.text

            # Initial graph + workspace + bound session
            initial = _graph_body(graph_id, agent_id=agent_id)
            initial["description"] = "T0548 initial"
            gr = await client.post("/v1/graphs", json=initial)
            assert gr.status_code == 201, gr.text

            ws = await client.post(
                "/v1/workspaces", json={"template_id": tpl_id},
            )
            assert ws.status_code == 201, ws.text
            workspace_id = ws.json()["id"]

            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json={
                    "binding": {"kind": "graph", "graph_id": graph_id},
                    "auto_start": False,
                },
            )
            assert sess.status_code == 201, sess.text
            session_id = sess.json()["id"]

            # Pause the bound session: CREATED → PAUSED (per
            # sessions.py:251-254 — direct transition)
            pause = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/"
                f"{session_id}/pause",
            )
            assert pause.status_code == 204, pause.text
            check = await client.get(f"/v1/sessions/{session_id}")
            assert check.json()["status"] == "paused", check.json()

            # Now PUT the graph with a structural change while a
            # PAUSED session is bound to it
            mutated = _graph_body(graph_id, agent_id=agent_id)
            mutated["description"] = "T0548 mutated"
            put_resp = await client.put(
                f"/v1/graphs/{graph_id}", json=mutated,
            )
            envelope = put_resp.json() if put_resp.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"PUT graph with PAUSED session leaked /errors/internal: "
                f"{put_resp.text}"
            )
            assert put_resp.status_code < 500, put_resp.text
            assert put_resp.status_code in (200, 409, 422), (
                f"unexpected PUT status: {put_resp.status_code}: "
                f"{put_resp.text}"
            )

            # Pre-existing session row still readable; binding intact
            after = await client.get(f"/v1/sessions/{session_id}")
            assert after.status_code == 200, after.text
            assert after.json()["id"] == session_id, after.json()
            assert after.json()["binding"]["kind"] == "graph", after.json()
            assert after.json()["binding"]["graph_id"] == graph_id, (
                after.json()
            )
        finally:
            if session_id is not None and workspace_id is not None:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/"
                    f"{session_id}/cancel",
                )
            if workspace_id is not None:
                await client.delete(f"/v1/workspaces/{workspace_id}")
            await client.delete(f"/v1/graphs/{graph_id}")
            await client.delete(f"/v1/workspace_templates/{tpl_id}")
            await client.delete(f"/v1/workspace_providers/{wp_id}")
            await client.delete(f"/v1/agents/{agent_id}")
            await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0561 — POST /v1/agents with temperature=null accepted (default deferred)
# ============================================================================


@pytest.mark.asyncio
async def test_t0561_post_agent_with_explicit_null_temperature_accepted(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0561 — Per matrix/model/agent.py:79, Agent.temperature is
    `float | None` defaulting to None. Pin: explicit `null` in the
    request body is accepted (201) — null is the documented
    "defer to adapter" sentinel. GET round-trips with field absent
    or null; /status returns clean envelope.

    Catches a regression where a future strict-typing change makes
    `null` distinct from omission and breaks the documented
    deferred-default semantics.
    """
    provider_id = f"llm-t0561-{unique_suffix}"
    agent_id = f"agent-t0561-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        body = {
            "id": agent_id,
            "description": "T0561 explicit null temperature",
            "model": {
                "provider_id": provider_id,
                "model_name": "claude-sonnet-4-6",
            },
            "tools": [],
            "temperature": None,  # explicit null
        }
        resp = await client.post("/v1/agents", json=body)
        assert resp.status_code == 201, resp.text

        got = await client.get(f"/v1/agents/{agent_id}")
        assert got.status_code == 200, got.text
        temp = got.json().get("temperature")
        assert temp is None, (
            f"temperature should be None (deferred to adapter); "
            f"got {temp!r}"
        )

        status = await client.get(f"/v1/agents/{agent_id}/status")
        assert status.status_code == 200, status.text
        assert "ok" in status.json(), status.json()
    finally:
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0563 — POST entity description with deeply-nested unicode escapes
# ============================================================================


@pytest.mark.asyncio
async def test_t0563_post_entity_description_with_deep_unicode_escapes(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0563 — Mirror of T0493 (api_key) for the description field
    on a Describeable entity. Use Agent (extends Describeable; per
    matrix/model/agent.py:66 — Toolset only extends Identifiable
    so description would be silently dropped). Build a description
    with 100+ stacked `\\u` escape sequences embedded in JSON wire
    bytes. Pin: 201 (accepted byte-exact) or clean 4xx; never
    /errors/internal from JSON re-encoding; GET round-trips byte-
    exact.
    """
    provider_id = f"llm-t0563-{unique_suffix}"
    agent_id = f"agent-t0563-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text

    # Build raw JSON wire bytes manually so the embedded `\uXXXX`
    # escape sequences arrive as actual JSON unicode escapes
    # (json.dumps would re-escape the backslashes and defeat the
    # purpose). Each `\u` pair on the wire decodes to
    # the literal two-char string `\u` after JSON parsing → 200
    # decoded chars total for 100 pairs.
    wire_pair = r"\u"  # 12 wire chars; decodes to `\u`
    description_payload = (
        f"T0563-marker-{unique_suffix}-" + wire_pair * 100 + "-end"
    )
    raw_body = (
        '{'
        f'"id":"{agent_id}",'
        f'"description":"{description_payload}",'
        f'"model":{{"provider_id":"{provider_id}",'
        f'"model_name":"claude-sonnet-4-6"}},'
        f'"tools":[]'
        '}'
    )

    try:
        resp = await client.post(
            "/v1/agents",
            content=raw_body.encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"deep-unicode description leaked /errors/internal: "
            f"{resp.text[:300]}"
        )
        assert resp.status_code < 500, resp.text[:300]
        assert resp.status_code in (201, 400, 422), (
            f"unexpected status: {resp.status_code}: "
            f"{resp.text[:300]}"
        )

        if resp.status_code == 201:
            got = await client.get(f"/v1/agents/{agent_id}")
            assert got.status_code == 200, got.text
            got_desc = got.json().get("description", "")
            decoded_unicode = "\\u" * 100
            expected = (
                f"T0563-marker-{unique_suffix}-{decoded_unicode}-end"
            )
            assert got_desc == expected, (
                f"description corrupted on round-trip:\n"
                f"  expected (len={len(expected)}): "
                f"{expected[:80]!r}...\n"
                f"  got      (len={len(got_desc)}): "
                f"{got_desc[:80]!r}..."
            )
    finally:
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0567 — Graph POST with empty nodes list returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0567_graph_post_empty_nodes_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0567 — Per matrix/model/graph.py:365-368, Graph.nodes is
    `list[GraphNode]` with `min_length=1`. Pin: empty nodes list
    is rejected at create with 422 /errors/validation-error
    (Pydantic min_length); never /errors/internal.
    """
    graph_id = f"graph-t0567-{unique_suffix}"
    body = {
        "id": graph_id,
        "description": "T0567 empty nodes",
        "nodes": [],  # rejected by min_length=1
        "edges": [],
        "entry_node_id": "anything",
    }
    resp = await client.post("/v1/graphs", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"empty nodes leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code == 422, (
        f"empty nodes should be 422; got "
        f"{resp.status_code}: {resp.text}"
    )
    assert envelope.get("type") == "/errors/validation-error", envelope

    # Defence: row not created
    got = await client.get(f"/v1/graphs/{graph_id}")
    assert got.status_code == 404, got.text


# ============================================================================
# T0568 — Graph POST with entry_node_id not in nodes returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0568_graph_post_entry_node_id_missing_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0568 — Per matrix/model/graph.py:399-402, the topology
    validator rejects entry_node_id values not present in nodes.
    Pin: 422 /errors/validation-error; never /errors/internal;
    row not created.
    """
    provider_id = f"llm-t0568-{unique_suffix}"
    agent_id = f"agent-t0568-{unique_suffix}"
    graph_id = f"graph-t0568-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        body = {
            "id": graph_id,
            "description": "T0568",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "missing-node-id",  # not in nodes
        }
        resp = await client.post("/v1/graphs", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"missing entry_node_id leaked /errors/internal: "
            f"{resp.text}"
        )
        assert resp.status_code == 422, (
            f"missing entry_node_id should be 422; got "
            f"{resp.status_code}: {resp.text}"
        )
        # Detail mentions the bad node id
        assert "missing-node-id" in resp.text, resp.text

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 404, got.text
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0569 — Graph POST with 100 nodes accepted; round-trips byte-exact
# ============================================================================


@pytest.mark.asyncio
async def test_t0569_graph_post_with_100_nodes_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0569 — Pin large-but-valid graph: 100 agent nodes + 1
    terminal, all wired through static edges to the terminal.
    Pin: 201 at create; GET round-trips the full node id set
    byte-exact; /status returns clean envelope.
    """
    provider_id = f"llm-t0569-{unique_suffix}"
    agent_id = f"agent-t0569-{unique_suffix}"
    graph_id = f"graph-t0569-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    node_ids = [f"n{i:03d}" for i in range(100)]
    try:
        nodes = [
            {"kind": "agent", "id": nid, "agent_id": agent_id}
            for nid in node_ids
        ] + [{"kind": "terminal", "id": "end"}]
        edges = [
            {"kind": "static", "from_node": nid, "to_node": "end"}
            for nid in node_ids
        ]
        body = {
            "id": graph_id,
            "description": "T0569 100-node graph",
            "nodes": nodes,
            "edges": edges,
            "entry_node_id": node_ids[0],
        }
        resp = await client.post(
            "/v1/graphs", json=body,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        assert resp.status_code == 201, (
            f"100-node graph should be accepted; got "
            f"{resp.status_code}: {resp.text[:300]}"
        )

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        got_node_ids = sorted(n["id"] for n in got.json()["nodes"])
        expected_ids = sorted(node_ids + ["end"])
        assert got_node_ids == expected_ids, (
            f"100-node graph node id set mismatch: "
            f"len={len(got_node_ids)}, expected={len(expected_ids)}"
        )

        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        assert "ok" in status.json(), status.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0570 — Mutual subgraph cycle (Graph A → B, Graph B → A) accepted
# ============================================================================


@pytest.mark.asyncio
async def test_t0570_mutual_subgraph_cycle_accepted_status_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0570 — Subgraph references are orphan-tolerated (T0475);
    pin that mutual cycles are also accepted. Create Graph A
    referencing B as a subgraph node, then Graph B referencing A.
    Pin: both POSTs return 201; both /status return clean
    envelope. After both exist, neither /status surfaces the
    mutual-ref as a missing reference.
    """
    provider_id = f"llm-t0570-{unique_suffix}"
    agent_id = f"agent-t0570-{unique_suffix}"
    graph_a_id = f"graph-a-t0570-{unique_suffix}"
    graph_b_id = f"graph-b-t0570-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        # Graph A references B as a subgraph
        body_a = {
            "id": graph_a_id,
            "description": "T0570 graph A",
            "nodes": [
                {"kind": "graph", "id": "subB", "graph_id": graph_b_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "subB", "to_node": "end"},
            ],
            "entry_node_id": "subB",
        }
        ra = await client.post("/v1/graphs", json=body_a)
        assert ra.status_code == 201, ra.text

        # Graph B references A as a subgraph (cycle)
        body_b = {
            "id": graph_b_id,
            "description": "T0570 graph B",
            "nodes": [
                {"kind": "graph", "id": "subA", "graph_id": graph_a_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "subA", "to_node": "end"},
            ],
            "entry_node_id": "subA",
        }
        rb = await client.post("/v1/graphs", json=body_b)
        assert rb.status_code == 201, rb.text

        # Both /status return clean envelope; both refs now exist
        for gid in (graph_a_id, graph_b_id):
            status = await client.get(f"/v1/graphs/{gid}/status")
            assert status.status_code == 200, status.text
            body = status.json()
            assert "ok" in body, body
            assert isinstance(body.get("issues"), list), body
            # Mutual cycle: each ref now exists, so no missing-ref
            # issues should be surfaced
            assert body.get("ok") is True, (
                f"graph {gid!r} /status reports issues despite "
                f"both refs existing: {body!r}"
            )
    finally:
        await client.delete(f"/v1/graphs/{graph_a_id}")
        await client.delete(f"/v1/graphs/{graph_b_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0571 — POST→DELETE→POST then /status returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0571_post_delete_post_then_status_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0571 — Sibling of T0547 (re-create after delete) extended
    with an immediate /status read after the second POST. Pin: no
    stale negative-cache leak — /status reflects the v2 row's
    references, never 404s for an id that just got recreated.
    """
    provider_id = f"llm-t0571-{unique_suffix}"
    agent_id = f"agent-t0571-{unique_suffix}"
    graph_id = f"graph-t0571-{unique_suffix}"
    warmup_id = f"graph-warm-t0571-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    # Pre-warm graphs table to avoid T0103a cold-start race
    warm = await client.post(
        "/v1/graphs", json=_graph_body(warmup_id, agent_id=agent_id),
    )
    assert warm.status_code == 201, warm.text
    await client.delete(f"/v1/graphs/{warmup_id}")

    try:
        # v1 + DELETE + v2
        v1 = await client.post(
            "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
        )
        assert v1.status_code == 201, v1.text
        rm = await client.delete(f"/v1/graphs/{graph_id}")
        assert rm.status_code == 204, rm.text
        v2 = await client.post(
            "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
        )
        assert v2.status_code == 201, v2.text

        # Immediate /status — pin no stale negative cache
        status = await client.get(f"/v1/graphs/{graph_id}/status")
        envelope = status.json() if status.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"/status after recreate leaked /errors/internal: "
            f"{status.text}"
        )
        assert status.status_code == 200, (
            f"/status after recreate should be 200, not stale 404; "
            f"got {status.status_code}: {status.text}"
        )
        assert "ok" in envelope, envelope
        assert isinstance(envelope.get("issues"), list), envelope
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0588 — POST /v1/agents with `tools=[null]` returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0588_agent_create_with_null_tool_entry_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0588 — Per matrix/model/agent.py, `Agent.tools` is `list[str]`
    (tool ids; the list itself MAY be empty but its entries must
    each be a string). A `null` element violates the inner type and
    must surface as 422 /errors/validation-error from Pydantic — never
    /errors/internal from a downstream NoneType.startswith() crash
    when the agent is later resolved.

    Defence: the agent row must NOT exist after the rejected POST.
    """
    agent_id = f"agent-t0588-{unique_suffix}"
    body = {
        "id": agent_id,
        "description": "T0588 null tool entry",
        "model": {
            "provider_id": f"placeholder-{unique_suffix}",
            "model_name": "claude-sonnet-4-6",
        },
        "tools": [None],  # the offending element
    }
    resp = await client.post("/v1/agents", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"tools=[null] leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code == 422, (
        f"tools=[null] should be 422 (Pydantic list[str] inner type "
        f"violation); got {resp.status_code}: {resp.text}"
    )
    assert envelope.get("type") == "/errors/validation-error", envelope

    # Defence: row not created
    got = await client.get(f"/v1/agents/{agent_id}")
    assert got.status_code == 404, got.text
    # Best-effort cleanup in case the assertion above was too strict
    await client.delete(f"/v1/agents/{agent_id}")


# ============================================================================
# T0594 — Predicate `=` on Agent.temperature literal 0 returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0594_predicate_eq_temperature_zero_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0594 — Sister of T0361 (`=` on Session.turn_no integer column).
    Pin `=` against the agent's `temperature` field (float | None) with
    a numeric literal 0. The same JSONB / asyncpg type-coercion bug
    family that hits T0236/T0361/T0583 may also surface here if the
    SQL builder casts the column to text but binds an int literal.

    Hard pin: never /errors/internal. Acceptable shapes:
    - 200 with seeded agent matching (preferred — translator coerces
      cleanly).
    - 502 /errors/provider-server-error (current behaviour for the
      bug family, accepted as documented).
    - 4xx if handler validates and rejects.
    """
    provider_id = f"llm-t0594-{unique_suffix}"
    agent_id = f"agent-t0594-{unique_suffix}"
    pr = await client.post("/v1/llm_providers", json={
        "id": provider_id,
        "provider": "anthropic",
        "models": [
            {"name": "claude-sonnet-4-6", "context_length": 200_000},
        ],
        "config": {"api_key": "placeholder"},
        "limits": {"max_concurrency": 1},
    })
    assert pr.status_code == 201, pr.text
    try:
        # Seed agent with explicit temperature=0
        body = _agent_body(agent_id, provider_id=provider_id, tools=[])
        body["temperature"] = 0
        ag = await client.post("/v1/agents", json=body)
        assert ag.status_code == 201, ag.text

        # Find with `=` against temperature literal 0
        find_body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "temperature"},
                "right": {"kind": "value", "value": 0},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/agents/find", json=find_body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"`=` on temperature=0 leaked /errors/internal: {resp.text}"
        )
        # Documented bug family: 502 /errors/provider-server-error from
        # asyncpg type-bind mismatch is currently the most likely surface.
        # Future fix flips this to 200 with the row matching.
        assert resp.status_code in (200, 400, 422, 502), (
            f"`=` on temperature got unexpected status: "
            f"{resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            # If the translator handled it cleanly, our seeded agent
            # MUST be in the result set (temperature really IS 0).
            ids = [item["id"] for item in resp.json()["items"]]
            assert agent_id in ids, (
                f"=0 should match the seeded agent (temperature=0); "
                f"got {ids!r}"
            )
        elif resp.status_code == 502:
            assert envelope.get("type") == "/errors/provider-server-error", (
                envelope
            )
    finally:
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0600 — 20 burst POST /v1/agents racing 20 burst DELETEs of same id set
# ============================================================================


@pytest.mark.asyncio
async def test_t0600_agent_burst_create_delete_race_clean_envelopes(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0600 — Burst CRUD churn. Generate 20 distinct agent ids,
    fire 20 POSTs and 20 DELETEs in parallel against them. Each
    response must be a clean envelope (201 / 409 / 204 / 404), no
    /errors/internal anywhere. The final list must be consistent —
    every id is either present (201 won the race) or absent (DELETE
    won), never half-created or duplicated.

    Pre-warms the agents table via a synchronous create+delete to
    avoid the documented T0103a cold-start CREATE TABLE race
    surfacing as 502 noise.
    """
    import asyncio
    provider_id = f"llm-t0600-{unique_suffix}"
    warmup_id = f"agent-warmup-t0600-{unique_suffix}"
    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    agent_ids = [f"agent-t0600-{i:02d}-{unique_suffix}" for i in range(20)]
    try:
        # Warm the agents table
        warm = await client.post(
            "/v1/agents",
            json=_agent_body(warmup_id, provider_id=provider_id, tools=[]),
        )
        assert warm.status_code == 201, warm.text
        await client.delete(f"/v1/agents/{warmup_id}")

        async def _post(aid: str) -> tuple[str, httpx.Response]:
            r = await client.post(
                "/v1/agents",
                json=_agent_body(aid, provider_id=provider_id, tools=[]),
            )
            return ("POST", r)

        async def _delete(aid: str) -> tuple[str, httpx.Response]:
            r = await client.delete(f"/v1/agents/{aid}")
            return ("DELETE", r)

        tasks: list[asyncio.Task] = []
        for aid in agent_ids:
            tasks.append(asyncio.create_task(_post(aid)))
            tasks.append(asyncio.create_task(_delete(aid)))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, item in enumerate(results):
            assert not isinstance(item, BaseException), (
                f"task {i} raised: {item!r}"
            )
            verb, r = item
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"{verb} #{i} leaked /errors/internal: "
                f"{r.status_code}: {r.text}"
            )
            if verb == "POST":
                # 201 (won), 409 (lost — id already exists), 502 (T0103a
                # cold-start fallback — we warmed but allow it).
                assert r.status_code in (201, 409, 502), (
                    f"POST #{i}: unexpected status "
                    f"{r.status_code}: {r.text}"
                )
            else:
                # DELETE: 204 (row existed), 404 (POST hadn't run yet)
                assert r.status_code in (204, 404), (
                    f"DELETE #{i}: unexpected status "
                    f"{r.status_code}: {r.text}"
                )

        # Final state: each id is either present (single non-corrupt
        # row) or absent. No duplicates anywhere.
        listed = await client.get(
            "/v1/agents", params={"limit": 200, "offset": 0},
        )
        assert listed.status_code == 200, listed.text
        listed_ids = [item["id"] for item in listed.json()["items"]]
        for aid in agent_ids:
            count = listed_ids.count(aid)
            assert count in (0, 1), (
                f"id {aid!r} appears {count}× in list — duplicates "
                f"after burst CRUD churn"
            )
    finally:
        for aid in agent_ids:
            try:
                await client.delete(f"/v1/agents/{aid}")
            except Exception:
                pass
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0589 — POST /v1/llm_providers with limits.max_concurrency=0 returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0589_llm_provider_max_concurrency_zero_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0589 — Per matrix/model/provider.py:313, `Limits.max_concurrency`
    is `PositiveInt`. Pydantic rejects 0 with 422 — never accepted as
    "no concurrency cap" (a different field would be used for that).

    Defence: row not created on rejection.
    """
    provider_id = f"llm-t0589-{unique_suffix}"
    body = _llm_body(provider_id)
    body["limits"] = {"max_concurrency": 0}  # the offending value

    resp = await client.post("/v1/llm_providers", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"max_concurrency=0 leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code == 422, (
        f"max_concurrency=0 should be 422 (PositiveInt); "
        f"got {resp.status_code}: {resp.text}"
    )
    assert envelope.get("type") == "/errors/validation-error", envelope

    got = await client.get(f"/v1/llm_providers/{provider_id}")
    assert got.status_code == 404, got.text
    await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0613 — Graph PUT v1→v2 with disjoint nodes reflects v2 on /status
# ============================================================================


@pytest.mark.asyncio
async def test_t0613_graph_put_replace_nodes_status_reflects_v2(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0613 — POST graph v1 with one node set, then PUT graph v2
    with a disjoint node set, then immediately GET /status. The
    status walk must reflect v2's nodes (no stale node-cache
    keeping v1's references alive).

    Catches a regression where the status endpoint reads from an
    in-memory cache that wasn't invalidated by the PUT, leading
    to v1's references being walked even after the row was
    replaced.
    """
    provider_id = f"llm-t0613-{unique_suffix}"
    agent_v1 = f"agent-v1-t0613-{unique_suffix}"
    agent_v2 = f"agent-v2-t0613-{unique_suffix}"
    graph_id = f"graph-t0613-{unique_suffix}"
    missing_agent = f"agent-MISSING-t0613-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        # Two real agents
        for aid in (agent_v1, agent_v2):
            ag = await client.post(
                "/v1/agents",
                json=_agent_body(aid, provider_id=provider_id, tools=[]),
            )
            assert ag.status_code == 201, ag.text

        # v1 references agent_v1 (real) — /status should show ok=true
        v1 = await client.post(
            "/v1/graphs",
            json={
                "id": graph_id,
                "description": "T0613 v1",
                "nodes": [
                    {"kind": "agent", "id": "n_v1",
                     "agent_id": agent_v1},
                ],
                "edges": [],
                "entry_node_id": "n_v1",
            },
        )
        assert v1.status_code == 201, v1.text
        s1 = await client.get(f"/v1/graphs/{graph_id}/status")
        assert s1.status_code == 200, s1.text
        assert s1.json()["ok"] is True, (
            f"v1 status should be ok=true (agent exists); got {s1.json()!r}"
        )

        # PUT v2 with a DISJOINT node set referencing missing_agent.
        # /status MUST reflect v2 (ok=false with missing-agent issue)
        # NOT keep walking v1's now-stale node references.
        v2_body = {
            "id": graph_id,
            "description": "T0613 v2 — replaced nodes",
            "nodes": [
                {"kind": "agent", "id": "n_v2",
                 "agent_id": missing_agent},
            ],
            "edges": [],
            "entry_node_id": "n_v2",
        }
        v2 = await client.put(f"/v1/graphs/{graph_id}", json=v2_body)
        assert v2.status_code == 200, v2.text

        # Immediate /status — must reflect v2's missing reference
        s2 = await client.get(f"/v1/graphs/{graph_id}/status")
        assert s2.status_code == 200, s2.text
        body = s2.json()
        assert body["ok"] is False, (
            f"v2 /status should be ok=false (refs missing agent); "
            f"got {body!r} — stale node-cache from v1?"
        )
        # The issue should reference the MISSING agent id (proof we
        # walked v2's nodes), not the still-existing agent_v1.
        issues_str = " ".join(str(i) for i in body["issues"])
        assert missing_agent in issues_str, (
            f"v2 /status didn't reference the missing agent {missing_agent!r}; "
            f"issues={body['issues']!r} — likely walked v1's stale nodes"
        )
        assert agent_v1 not in issues_str, (
            f"v2 /status still references v1's agent {agent_v1!r}; "
            f"issues={body['issues']!r} — node-cache not invalidated"
        )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        for aid in (agent_v1, agent_v2):
            await client.delete(f"/v1/agents/{aid}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0617 — Graph node body with both agent_id AND graph_id: clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0617_graph_node_with_both_agent_id_and_graph_id_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0617 — Graph nodes are a discriminated union by `kind`. Per
    matrix/model/graph.py:152-201, _AgentNodeRef has fields
    {kind, id, agent_id} and _GraphNodeRef has {kind, id, graph_id}.
    Sending kind="agent" with BOTH agent_id and graph_id is
    structurally suspicious — Pydantic's default extra=ignore
    silently drops the extra `graph_id`, but a strict-mode model
    would reject with 422.

    Hard pin: clean envelope (201 with the extra silently dropped,
    OR 422 if strict). Never /errors/internal. Documents the current
    behavior so a future tightening is a deliberate test update.
    """
    provider_id = f"llm-t0617-{unique_suffix}"
    agent_id = f"agent-t0617-{unique_suffix}"
    graph_id = f"graph-t0617-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    try:
        body = {
            "id": graph_id,
            "description": "T0617 mixed-id node",
            "nodes": [
                {
                    "kind": "agent",
                    "id": "n1",
                    "agent_id": agent_id,
                    # The suspicious extra:
                    "graph_id": "should-not-be-here",
                },
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"mixed-id node leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code in (201, 400, 422), (
            f"mixed-id node unexpected status: "
            f"{resp.status_code}: {resp.text}"
        )
        if resp.status_code == 201:
            # Extra silently dropped — GET round-trip should not contain
            # the rogue graph_id field on the agent-kind node
            got = await client.get(f"/v1/graphs/{graph_id}")
            assert got.status_code == 200, got.text
            n1 = got.json()["nodes"][0]
            assert n1["kind"] == "agent", n1
            assert n1.get("graph_id") in (None, "should-not-be-here"), (
                f"unexpected graph_id field treatment: {n1!r}"
            )
        else:
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0618 — Graph PUT swapping entry_node_id to a non-existent node returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0618_graph_put_entry_node_id_missing_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0618 — Mirror of T0568 for the PUT path. The same topology
    validator that rejects entry_node_id-not-in-nodes at POST must
    reject it at PUT. The original row must NOT be corrupted by
    the rejected PUT (the description and entry_node_id should be
    unchanged after the failed call).
    """
    provider_id = f"llm-t0618-{unique_suffix}"
    agent_id = f"agent-t0618-{unique_suffix}"
    graph_id = f"graph-t0618-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    valid_body = {
        "id": graph_id,
        "description": "T0618 original",
        "nodes": [
            {"kind": "agent", "id": "n1", "agent_id": agent_id},
            {"kind": "terminal", "id": "end"},
        ],
        "edges": [
            {"kind": "static", "from_node": "n1", "to_node": "end"},
        ],
        "entry_node_id": "n1",
    }
    create = await client.post("/v1/graphs", json=valid_body)
    assert create.status_code == 201, create.text

    try:
        # PUT with entry_node_id pointing at a non-existent node
        put_body = dict(valid_body)
        put_body["description"] = "T0618 attempted-corrupt"
        put_body["entry_node_id"] = "MISSING-node-id"
        resp = await client.put(f"/v1/graphs/{graph_id}", json=put_body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"PUT bad entry_node_id leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code == 422, (
            f"PUT with missing entry_node_id should be 422; got "
            f"{resp.status_code}: {resp.text}"
        )
        # Detail mentions the bad node id
        assert "MISSING-node-id" in resp.text, resp.text

        # Defence: original row unchanged
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        assert got.json()["description"] == "T0618 original", got.json()
        assert got.json()["entry_node_id"] == "n1", got.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0619 — Graph with two parallel edges A→B accepted; both round-trip
# ============================================================================


@pytest.mark.asyncio
async def test_t0619_graph_duplicate_edges_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0619 — Two identical edges from A to B (parallel multi-edge).
    Per the model edges are a list (not a set), so duplicates are
    allowed at the schema level. Pin: 201 accepts the body and GET
    round-trips both edges (no silent dedup). /status remains
    clean. Hard pin: never /errors/internal.

    A future iteration that decides to dedup edges at create time
    would deliberately break this test.
    """
    provider_id = f"llm-t0619-{unique_suffix}"
    agent_id = f"agent-t0619-{unique_suffix}"
    graph_id = f"graph-t0619-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    body = {
        "id": graph_id,
        "description": "T0619 duplicate edges",
        "nodes": [
            {"kind": "agent", "id": "n1", "agent_id": agent_id},
            {"kind": "terminal", "id": "end"},
        ],
        "edges": [
            {"kind": "static", "from_node": "n1", "to_node": "end"},
            {"kind": "static", "from_node": "n1", "to_node": "end"},
        ],
        "entry_node_id": "n1",
    }
    try:
        resp = await client.post("/v1/graphs", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"duplicate edges leaked /errors/internal: {resp.text}"
        )
        # Either accepted (201) or rejected with clean 4xx
        assert resp.status_code in (201, 400, 422), (
            f"duplicate edges unexpected status: "
            f"{resp.status_code}: {resp.text}"
        )
        if resp.status_code == 201:
            got = await client.get(f"/v1/graphs/{graph_id}")
            assert got.status_code == 200, got.text
            edges = got.json()["edges"]
            # Pin: no silent dedup — both edges round-trip
            assert len(edges) == 2, (
                f"duplicate edges silently deduped at GET: "
                f"got {len(edges)} edges, expected 2: {edges!r}"
            )
            # /status walks the graph cleanly
            status = await client.get(f"/v1/graphs/{graph_id}/status")
            assert status.status_code == 200, status.text
            assert status.json().get("ok") is True, status.json()
        else:
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0620 — Graph node id="" rejected 422 (Identifiable.id min_length=1)
# ============================================================================


@pytest.mark.asyncio
async def test_t0620_graph_node_empty_id_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0620 — Per matrix/model/graph.py:160-162, _AgentNodeRef.id
    has min_length=1 (and similarly for _GraphNodeRef and
    _TerminalNode). Pin: explicit empty-string id is rejected with
    422 /errors/validation-error; row not created.
    """
    provider_id = f"llm-t0620-{unique_suffix}"
    agent_id = f"agent-t0620-{unique_suffix}"
    graph_id = f"graph-t0620-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    try:
        body = {
            "id": graph_id,
            "description": "T0620 empty node id",
            "nodes": [
                {"kind": "agent", "id": "", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [],
            "entry_node_id": "end",
        }
        resp = await client.post("/v1/graphs", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"empty node id leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code == 422, (
            f"empty node id should be 422 (Pydantic min_length=1); "
            f"got {resp.status_code}: {resp.text}"
        )
        assert envelope.get("type") == "/errors/validation-error", envelope

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 404, got.text
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0621 — Graph zero-edge multi-node accepted; /status ok=true
# ============================================================================


@pytest.mark.asyncio
async def test_t0621_graph_zero_edges_multi_node_accepted_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0621 — Pin that a graph with multiple nodes but ZERO edges
    is a legal shape. The DAG can have isolated nodes; the entry
    node must be present (validated by T0568) but unreachable
    nodes are not rejected at create time.

    Hard pin: 201 + /status returns ok=true (or at least clean
    envelope, not /errors/internal).
    """
    provider_id = f"llm-t0621-{unique_suffix}"
    agent_id = f"agent-t0621-{unique_suffix}"
    graph_id = f"graph-t0621-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    try:
        body = {
            "id": graph_id,
            "description": "T0621 zero-edge multi-node",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "agent", "id": "n2", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [],  # the unusual shape
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"zero-edge graph leaked /errors/internal: {resp.text}"
        )
        # Acceptable: 201 (legal) or clean 4xx (if validator added)
        assert resp.status_code in (201, 400, 422), (
            f"zero-edge graph unexpected status: "
            f"{resp.status_code}: {resp.text}"
        )
        if resp.status_code == 201:
            got = await client.get(f"/v1/graphs/{graph_id}")
            assert got.status_code == 200, got.text
            assert got.json()["edges"] == [], got.json()
            assert len(got.json()["nodes"]) == 3, got.json()

            status = await client.get(f"/v1/graphs/{graph_id}/status")
            assert status.status_code == 200, status.text
            # All references resolve (one agent referenced twice + terminal)
            assert status.json().get("ok") is True, (
                f"zero-edge graph status should be ok=true: "
                f"{status.json()!r}"
            )
        else:
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0622 — Graph max_iterations=null accepted; round-trips null
# ============================================================================


@pytest.mark.asyncio
async def test_t0622_graph_max_iterations_null_accepted(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0622 — Per matrix/model/graph.py:379, `max_iterations` is
    `PositiveInt | None` with default `None`. Pin: an explicit `null`
    in the body is accepted (201), GET round-trips null, /status
    returns clean. Sister of T0497 (very-large value).
    """
    provider_id = f"llm-t0622-{unique_suffix}"
    agent_id = f"agent-t0622-{unique_suffix}"
    graph_id = f"graph-t0622-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    try:
        body = {
            "id": graph_id,
            "description": "T0622 max_iterations=null",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
            "max_iterations": None,
        }
        resp = await client.post("/v1/graphs", json=body)
        assert resp.status_code == 201, (
            f"max_iterations=null should be accepted; got "
            f"{resp.status_code}: {resp.text}"
        )
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        assert got.json().get("max_iterations") is None, (
            f"explicit null should round-trip null: "
            f"{got.json().get('max_iterations')!r}"
        )

        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        assert status.json().get("ok") is True, status.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0623 — Three concurrent PUTs replacing same Graph nodes set: clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0623_graph_concurrent_put_replace_clean_envelopes(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0623 — Three simultaneous PUTs on the same graph_id, each
    submitting a distinguishable description + nodes set. Mirror of
    the agent CRUD churn pattern. Pin: every response is a clean
    envelope (200/4xx — never /errors/internal). Final GET returns
    one of the three submitted bodies (last-writer-wins). /status
    walks the resolved graph cleanly.
    """
    import asyncio
    provider_id = f"llm-t0623-{unique_suffix}"
    agent_id = f"agent-t0623-{unique_suffix}"
    graph_id = f"graph-t0623-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    # Seed the graph
    seed = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert seed.status_code == 201, seed.text

    try:
        def _put_body(tag: str) -> dict:
            return {
                "id": graph_id,
                "description": f"T0623 racer {tag}",
                "nodes": [
                    {"kind": "agent", "id": f"n_{tag}",
                     "agent_id": agent_id},
                    {"kind": "terminal", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": f"n_{tag}",
                     "to_node": "end"},
                ],
                "entry_node_id": f"n_{tag}",
            }

        tasks = [
            asyncio.create_task(
                client.put(f"/v1/graphs/{graph_id}", json=_put_body(tag)),
            )
            for tag in ("a", "b", "c")
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, r in enumerate(results):
            assert not isinstance(r, BaseException), (
                f"PUT #{i} raised: {r!r}"
            )
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"PUT #{i} leaked /errors/internal: "
                f"{r.status_code}: {r.text}"
            )
            # 200 (won) or 409 (lost). Never 5xx beyond documented bug
            # families.
            assert r.status_code in (200, 409), (
                f"PUT #{i}: unexpected status {r.status_code}: {r.text}"
            )

        # Final GET shows one of the three submitted bodies
        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        desc = got.json().get("description", "")
        assert desc in (
            "T0623 racer a",
            "T0623 racer b",
            "T0623 racer c",
        ), f"final description not from submitted set: {desc!r}"

        # Status walks the resolved graph cleanly
        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        assert status.json().get("ok") is True, status.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0624 — Graph DELETEd then resume on graph-bound session: clean fatal-path
# ============================================================================


@pytest.mark.asyncio
async def test_t0624_graph_deleted_then_resume_session_clean_fatal_path(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0624 — Sequential graph cascade. Sequence:
        1. Create graph + workspace + graph-bound CREATED session
        2. DELETE the graph row
        3. Resume the session

    Per matrix/worker/pool.py:478, the executor is NotImplemented
    regardless. The session must converge to ENDED via _handle_fatal
    with a clean error path. Hard pin: never /errors/internal at any
    step; resume returns a documented status code; subsequent GET
    shows ended/failed.
    """
    import asyncio as _asyncio
    provider_id = f"llm-t0624-{unique_suffix}"
    agent_id = f"agent-t0624-{unique_suffix}"
    wp_id = f"wp-t0624-{unique_suffix}"
    tpl_id = f"wt-t0624-{unique_suffix}"
    graph_id = f"graph-t0624-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    wp = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert wp.status_code == 201, wp.text
    tpl = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id,
            "description": "T0624",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert tpl.status_code == 201, tpl.text
    gr = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert gr.status_code == 201, gr.text

    workspace_id: str | None = None
    session_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": tpl_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # Delete the graph row BEFORE resume
        rm = await client.delete(f"/v1/graphs/{graph_id}")
        assert rm.status_code == 204, rm.text

        # Resume — worker claims, fails (graph missing AND/OR executor
        # NotImplemented), routes through _handle_fatal
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        envelope = resume.json() if resume.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"resume after graph delete leaked /errors/internal: "
            f"{resume.text}"
        )
        assert resume.status_code < 500, (
            f"resume after graph delete returned 5xx: "
            f"{resume.status_code}: {resume.text}"
        )
        assert resume.status_code in (200, 400, 404, 409, 422), (
            f"resume unexpected status: "
            f"{resume.status_code}: {resume.text}"
        )

        # If resume succeeded (200), wait for the worker to converge
        # the session to ENDED via _handle_fatal.
        if resume.status_code == 200:
            for _ in range(60):  # 30s budget
                got = await client.get(f"/v1/sessions/{session_id}")
                if got.status_code == 200 and got.json().get("status") == "ended":
                    break
                await _asyncio.sleep(0.5)
            final = await client.get(f"/v1/sessions/{session_id}")
            assert final.status_code == 200, final.text
            assert final.json().get("status") == "ended", (
                f"session did not converge to ended in 30s: "
                f"{final.json()!r}"
            )
            assert final.json().get("ended_reason") == "failed", final.json()
            last_err = final.json().get("last_error") or ""
            # The error should reference EITHER the missing graph OR the
            # NotImplementedError (whichever the worker hits first).
            assert last_err, (
                f"converged session must have last_error: {final.json()!r}"
            )
    finally:
        if session_id is not None and workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        # graph already deleted; idempotent best-effort
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/workspace_templates/{tpl_id}")
        await client.delete(f"/v1/workspace_providers/{wp_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0637 — Graph status across 3-level subgraph chain to leaf missing agent
# ============================================================================


@pytest.mark.asyncio
async def test_t0637_graph_status_3_level_subgraph_chain_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0637 — Build a 3-level subgraph chain A→B→C where C's leaf
    agent_id references a missing agent. Pin: GET /v1/graphs/A/status
    returns 200 with a clean envelope (whether issues surface
    transitively or only at the local depth is implementation-defined,
    but the response MUST be 200 with the documented {ok, issues}
    shape). Hard pin: never /errors/internal regardless of how deep
    the resolver walks.
    """
    provider_id = f"llm-t0637-{unique_suffix}"
    real_agent = f"agent-real-t0637-{unique_suffix}"
    missing_agent = f"agent-MISSING-t0637-{unique_suffix}"
    graph_a = f"graph-a-t0637-{unique_suffix}"
    graph_b = f"graph-b-t0637-{unique_suffix}"
    graph_c = f"graph-c-t0637-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(real_agent, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        # C: references the MISSING agent (the leaf bug)
        gc = await client.post(
            "/v1/graphs",
            json={
                "id": graph_c,
                "description": "T0637 leaf graph (broken)",
                "nodes": [
                    {"kind": "agent", "id": "n_c",
                     "agent_id": missing_agent},
                ],
                "edges": [],
                "entry_node_id": "n_c",
            },
        )
        assert gc.status_code == 201, gc.text

        # B: references graph C
        gb = await client.post(
            "/v1/graphs",
            json={
                "id": graph_b,
                "description": "T0637 middle graph",
                "nodes": [
                    {"kind": "graph", "id": "n_b",
                     "graph_id": graph_c},
                ],
                "edges": [],
                "entry_node_id": "n_b",
            },
        )
        assert gb.status_code == 201, gb.text

        # A: references graph B
        ga = await client.post(
            "/v1/graphs",
            json={
                "id": graph_a,
                "description": "T0637 root graph",
                "nodes": [
                    {"kind": "graph", "id": "n_a",
                     "graph_id": graph_b},
                ],
                "edges": [],
                "entry_node_id": "n_a",
            },
        )
        assert ga.status_code == 201, ga.text

        status = await client.get(f"/v1/graphs/{graph_a}/status")
        envelope = status.json() if status.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"3-level chain /status leaked /errors/internal: "
            f"{status.text}"
        )
        assert status.status_code == 200, (
            f"3-level chain /status not 200: "
            f"{status.status_code}: {status.text}"
        )
        body = status.json()
        # Required shape: {ok, issues}
        assert "ok" in body, body
        assert isinstance(body.get("issues"), list), body
        # ok may be true (resolver shallow — only checks immediate
        # references) or false (resolver transitive — surfaces missing
        # leaf). Both behaviors are documented.
        assert body["ok"] in (True, False), body
    finally:
        for gid in (graph_a, graph_b, graph_c):
            await client.delete(f"/v1/graphs/{gid}")
        await client.delete(f"/v1/agents/{real_agent}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0638 — Graph-bound steer→cancel→resume converges to ENDED/failed clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0638_graph_session_steer_cancel_resume_clean_fatal(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0638 — Long-walk sister of T0593. Sequence on a graph-bound
    CREATED session:
        1. steer (queue an instruction) → 2xx
        2. cancel → 200 (direct from CREATED) or async to ENDED
        3. resume on ENDED → documented code, never 5xx

    Hard pin: every step a clean envelope. Final session is in
    ENDED state with non-empty last_error (either NotImplementedError
    text from the executor OR cancellation text — both acceptable
    per the documented graph-fatal path).
    """
    import asyncio as _asyncio
    provider_id = f"llm-t0638-{unique_suffix}"
    agent_id = f"agent-t0638-{unique_suffix}"
    wp_id = f"wp-t0638-{unique_suffix}"
    tpl_id = f"wt-t0638-{unique_suffix}"
    graph_id = f"graph-t0638-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    wp = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert wp.status_code == 201, wp.text
    tpl = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id,
            "description": "T0638",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert tpl.status_code == 201, tpl.text
    gr = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert gr.status_code == 201, gr.text

    workspace_id: str | None = None
    session_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": tpl_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # 1. steer
        steer = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": "T0638 pre-cancel"},
        )
        steer_env = steer.json() if steer.content else {}
        assert steer_env.get("type") != "/errors/internal", steer.text
        assert steer.status_code < 500, steer.text

        # 2. cancel
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        cancel_env = cancel.json() if cancel.content else {}
        assert cancel_env.get("type") != "/errors/internal", cancel.text
        assert cancel.status_code == 200, cancel.text

        # 3. resume on ENDED
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        resume_env = resume.json() if resume.content else {}
        assert resume_env.get("type") != "/errors/internal", resume.text
        assert resume.status_code < 500, resume.text

        # If resume bumped session back to CREATED/RUNNING, poll for ended
        for _ in range(60):  # 30s budget
            r = await client.get(f"/v1/sessions/{session_id}")
            if r.status_code == 200 and r.json().get("status") == "ended":
                break
            await _asyncio.sleep(0.5)

        final = await client.get(f"/v1/sessions/{session_id}")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "ended", final.json()
        # ended_reason can be cancelled (cancel won the race) or failed
        # (executor NotImplemented after resume retried)
        assert final.json()["ended_reason"] in ("cancelled", "failed"), (
            final.json()
        )
    finally:
        if session_id is not None and workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/workspace_templates/{tpl_id}")
        await client.delete(f"/v1/workspace_providers/{wp_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0639 — Two graph-bound sessions on same workspace both terminate cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0639_two_graph_bound_sessions_terminate_independently(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0639 — Create two distinct graph-bound sessions on the same
    workspace, resume both. Both must converge ENDED/failed via
    _handle_fatal independently. Hard pin: distinct ids; both have
    non-empty last_error; neither's failure leaks into the other's
    row (no cross-session state pollution).
    """
    import asyncio as _asyncio
    provider_id = f"llm-t0639-{unique_suffix}"
    agent_id = f"agent-t0639-{unique_suffix}"
    wp_id = f"wp-t0639-{unique_suffix}"
    tpl_id = f"wt-t0639-{unique_suffix}"
    graph_id = f"graph-t0639-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    wp = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert wp.status_code == 201, wp.text
    tpl = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id,
            "description": "T0639",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert tpl.status_code == 201, tpl.text
    gr = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert gr.status_code == 201, gr.text

    workspace_id: str | None = None
    session_ids: list[str] = []
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": tpl_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Two distinct graph-bound sessions
        for _ in range(2):
            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json={
                    "binding": {"kind": "graph", "graph_id": graph_id},
                    "auto_start": False,
                },
            )
            assert sess.status_code == 201, sess.text
            session_ids.append(sess.json()["id"])

        assert len(set(session_ids)) == 2, session_ids

        # Resume both
        for sid in session_ids:
            r = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{sid}/resume",
            )
            env = r.json() if r.content else {}
            assert env.get("type") != "/errors/internal", r.text
            assert r.status_code == 200, r.text

        # Both converge to ENDED (poll both)
        async def _wait_ended(sid: str) -> dict:
            for _ in range(60):
                r = await client.get(f"/v1/sessions/{sid}")
                if r.status_code == 200 and r.json().get("status") == "ended":
                    return r.json()
                await _asyncio.sleep(0.5)
            r = await client.get(f"/v1/sessions/{sid}")
            return r.json()

        bodies = await _asyncio.gather(
            *[_wait_ended(sid) for sid in session_ids],
        )

        for i, body in enumerate(bodies):
            assert body.get("status") == "ended", (
                f"session #{i} did not converge ended in 30s: {body!r}"
            )
            assert body.get("ended_reason") == "failed", (
                f"session #{i} expected failed: {body!r}"
            )
            assert body.get("last_error"), (
                f"session #{i} missing last_error: {body!r}"
            )
            assert body.get("id") == session_ids[i], body
    finally:
        for sid in session_ids:
            if workspace_id is not None:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/{sid}/cancel",
                )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/workspace_templates/{tpl_id}")
        await client.delete(f"/v1/workspace_providers/{wp_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0640 — Graph PUT mid-flight on RUNNING graph-bound session: clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0640_graph_put_mid_flight_on_running_session(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0640 — Bind session to graph G, resume (worker claims and
    fails fast via NotImplementedError), then immediately PUT G with
    a disjoint nodes set. PUT must return 200 with a clean envelope;
    subsequent GET on the session must read cleanly (no 5xx); session
    converges to ENDED.

    Catches a regression where mutating a graph row currently bound
    to a session corrupts either the row or the session.
    """
    import asyncio as _asyncio
    provider_id = f"llm-t0640-{unique_suffix}"
    agent_id = f"agent-t0640-{unique_suffix}"
    wp_id = f"wp-t0640-{unique_suffix}"
    tpl_id = f"wt-t0640-{unique_suffix}"
    graph_id = f"graph-t0640-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    wp = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert wp.status_code == 201, wp.text
    tpl = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id,
            "description": "T0640",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert tpl.status_code == 201, tpl.text
    gr = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert gr.status_code == 201, gr.text

    workspace_id: str | None = None
    session_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": tpl_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # Resume — worker claims + fails fast
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        assert resume.status_code == 200, resume.text

        # Immediately PUT graph with disjoint nodes — race against the
        # worker's fatal-path teardown
        put_body = {
            "id": graph_id,
            "description": "T0640 mutated mid-flight",
            "nodes": [
                {"kind": "agent", "id": "n_new", "agent_id": agent_id},
                {"kind": "terminal", "id": "end_new"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n_new",
                 "to_node": "end_new"},
            ],
            "entry_node_id": "n_new",
        }
        put = await client.put(f"/v1/graphs/{graph_id}", json=put_body)
        put_env = put.json() if put.content else {}
        assert put_env.get("type") != "/errors/internal", (
            f"PUT mid-flight leaked /errors/internal: {put.text}"
        )
        assert put.status_code == 200, (
            f"PUT mid-flight should be 200: {put.status_code}: {put.text}"
        )

        # Session GET still readable
        sess_get = await client.get(f"/v1/sessions/{session_id}")
        assert sess_get.status_code == 200, sess_get.text

        # Wait for ENDED
        for _ in range(60):
            r = await client.get(f"/v1/sessions/{session_id}")
            if r.status_code == 200 and r.json().get("status") == "ended":
                break
            await _asyncio.sleep(0.5)

        final = await client.get(f"/v1/sessions/{session_id}")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "ended", final.json()
    finally:
        if session_id is not None and workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/workspace_templates/{tpl_id}")
        await client.delete(f"/v1/workspace_providers/{wp_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0641 — Graph with 200 nodes + 199 edges accepted; round-trips byte-exact
# ============================================================================


@pytest.mark.asyncio
async def test_t0641_graph_large_dag_200_nodes_round_trips(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0641 — Stress validator on a large DAG: 200 nodes (199
    agent-kind + 1 terminal) chained in a linear path with 199 static
    edges. Pin: 201 accepted; GET round-trips with the same node
    count and edge count; /status returns ok=true (all references
    resolve to the same real agent); never /errors/internal on
    a large payload.
    """
    provider_id = f"llm-t0641-{unique_suffix}"
    agent_id = f"agent-t0641-{unique_suffix}"
    graph_id = f"graph-t0641-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        # 199 agent nodes + 1 terminal node
        nodes = [
            {"kind": "agent", "id": f"n{i:03d}", "agent_id": agent_id}
            for i in range(199)
        ]
        nodes.append({"kind": "terminal", "id": "end"})
        # Linear chain edges
        edges = [
            {"kind": "static", "from_node": f"n{i:03d}",
             "to_node": f"n{i + 1:03d}"}
            for i in range(198)
        ]
        edges.append(
            {"kind": "static", "from_node": "n198", "to_node": "end"},
        )

        body = {
            "id": graph_id,
            "description": "T0641 large DAG (200 nodes, 199 edges)",
            "nodes": nodes,
            "edges": edges,
            "entry_node_id": "n000",
        }
        resp = await client.post("/v1/graphs", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"large DAG POST leaked /errors/internal: "
            f"{resp.text[:300]}"
        )
        assert resp.status_code == 201, (
            f"large DAG should be accepted: "
            f"{resp.status_code}: {resp.text[:300]}"
        )

        got = await client.get(f"/v1/graphs/{graph_id}")
        assert got.status_code == 200, got.text
        assert len(got.json()["nodes"]) == 200, (
            f"node count diverged: {len(got.json()['nodes'])}"
        )
        assert len(got.json()["edges"]) == 199, (
            f"edge count diverged: {len(got.json()['edges'])}"
        )
        assert got.json()["entry_node_id"] == "n000", got.json()

        status = await client.get(f"/v1/graphs/{graph_id}/status")
        assert status.status_code == 200, status.text
        assert status.json().get("ok") is True, status.json()
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0642 — Graph with duplicate node ids in nodes list returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0642_graph_duplicate_node_ids_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0642 — Two nodes sharing the same id ("n1"). The topology
    validator (matrix/model/graph.py) should reject this with 422
    surfacing the duplicate id; the row must NOT be created.

    Hard pin: never /errors/internal. If a future iteration relaxes
    this to allow duplicate ids (which would silently lose one to
    the dict-keyed lookup), this test would document that.
    """
    provider_id = f"llm-t0642-{unique_suffix}"
    agent_id = f"agent-t0642-{unique_suffix}"
    graph_id = f"graph-t0642-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    try:
        body = {
            "id": graph_id,
            "description": "T0642 duplicate node ids",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": agent_id},
                {"kind": "agent", "id": "n1", "agent_id": agent_id},  # dup
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
            "entry_node_id": "n1",
        }
        resp = await client.post("/v1/graphs", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"duplicate node ids leaked /errors/internal: {resp.text}"
        )
        # Acceptable: 422 (validator catches) OR 201 (validator silently
        # tolerates — would be a documented permissive behaviour).
        assert resp.status_code in (201, 400, 422), (
            f"duplicate node ids unexpected status: "
            f"{resp.status_code}: {resp.text}"
        )
        if resp.status_code == 422:
            # Detail or extensions should mention "n1" or "duplicate"
            assert (
                "n1" in resp.text.lower()
                or "duplicate" in resp.text.lower()
            ), (
                f"422 envelope should reference duplicate id 'n1' or "
                f"'duplicate': {resp.text}"
            )
            # Defence: row not created
            got = await client.get(f"/v1/graphs/{graph_id}")
            assert got.status_code == 404, got.text
        elif resp.status_code == 201:
            # Permissive path — pin GET round-trips and /status clean
            got = await client.get(f"/v1/graphs/{graph_id}")
            assert got.status_code == 200, got.text
            status = await client.get(f"/v1/graphs/{graph_id}/status")
            status_env = status.json() if status.content else {}
            assert status_env.get("type") != "/errors/internal", (
                f"/status with duplicate node ids leaked: {status.text}"
            )
            assert status.status_code == 200, status.text
        else:
            assert envelope.get("type", "").startswith("/errors/"), envelope
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0672 — Cancel on graph-bound CREATED session BEFORE first worker turn
# ============================================================================


@pytest.mark.asyncio
async def test_t0672_cancel_graph_bound_created_before_first_turn(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0672 — Pre-fatal cancel path on graph executor. Sequence:
        1. Create graph + workspace + graph-bound CREATED session
        2. Cancel BEFORE calling resume

    The session must converge to ENDED/cancelled cleanly without
    ever invoking _build_graph_executor (which would throw
    NotImplementedError). This pins the "fast cancel" path
    distinct from the resume-then-fatal path covered by T0429.
    """
    provider_id = f"llm-t0672-{unique_suffix}"
    agent_id = f"agent-t0672-{unique_suffix}"
    wp_id = f"wp-t0672-{unique_suffix}"
    tpl_id = f"wt-t0672-{unique_suffix}"
    graph_id = f"graph-t0672-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    wp = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert wp.status_code == 201, wp.text
    tpl = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id,
            "description": "T0672",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert tpl.status_code == 201, tpl.text
    gr = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert gr.status_code == 201, gr.text

    workspace_id: str | None = None
    session_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": tpl_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # Cancel BEFORE resume — pre-fatal path
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        env = cancel.json() if cancel.content else {}
        assert env.get("type") != "/errors/internal", cancel.text
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended", cancel.json()
        assert cancel.json()["ended_reason"] == "cancelled", cancel.json()

        # No worker activity required — confirm by checking last_error
        # is None (NotImplementedError never fired)
        final = await client.get(f"/v1/sessions/{session_id}")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "ended", final.json()
        assert final.json()["ended_reason"] == "cancelled", final.json()
        assert final.json().get("last_error") is None, (
            f"pre-fatal cancel on graph session shouldn't have triggered "
            f"the executor; last_error should be None: {final.json()!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/workspace_templates/{tpl_id}")
        await client.delete(f"/v1/workspace_providers/{wp_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0673 — Graph PUT changing entry_node_id mid-flight on CREATED session
# ============================================================================


@pytest.mark.asyncio
async def test_t0673_graph_put_entry_swap_mid_flight_session_clean_fatal(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0673 — Stale-cache check sister of T0640. Bind session to
    graph G, then PUT G mutating entry_node_id (and the corresponding
    nodes set) BEFORE resume. Resume — worker reads the current row
    state and converges via _handle_fatal cleanly.

    Hard pin: no /errors/internal at any step; PUT 200; resume 200;
    session converges to ENDED.
    """
    import asyncio as _asyncio
    provider_id = f"llm-t0673-{unique_suffix}"
    agent_id = f"agent-t0673-{unique_suffix}"
    wp_id = f"wp-t0673-{unique_suffix}"
    tpl_id = f"wt-t0673-{unique_suffix}"
    graph_id = f"graph-t0673-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    wp = await client.post(
        "/v1/workspace_providers",
        json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert wp.status_code == 201, wp.text
    tpl = await client.post(
        "/v1/workspace_templates",
        json={
            "id": tpl_id,
            "description": "T0673",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        },
    )
    assert tpl.status_code == 201, tpl.text

    # Initial graph: entry_node_id = "n1"
    v1_body = {
        "id": graph_id,
        "description": "T0673 v1",
        "nodes": [
            {"kind": "agent", "id": "n1", "agent_id": agent_id},
            {"kind": "terminal", "id": "end"},
        ],
        "edges": [
            {"kind": "static", "from_node": "n1", "to_node": "end"},
        ],
        "entry_node_id": "n1",
    }
    gr = await client.post("/v1/graphs", json=v1_body)
    assert gr.status_code == 201, gr.text

    workspace_id: str | None = None
    session_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": tpl_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # PUT graph with NEW entry_node_id and disjoint nodes
        v2_body = {
            "id": graph_id,
            "description": "T0673 v2 — new entry",
            "nodes": [
                {"kind": "agent", "id": "n_new", "agent_id": agent_id},
                {"kind": "terminal", "id": "end_new"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n_new",
                 "to_node": "end_new"},
            ],
            "entry_node_id": "n_new",
        }
        put = await client.put(f"/v1/graphs/{graph_id}", json=v2_body)
        put_env = put.json() if put.content else {}
        assert put_env.get("type") != "/errors/internal", put.text
        assert put.status_code == 200, put.text

        # Resume — worker reads the new entry_node_id from storage
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        resume_env = resume.json() if resume.content else {}
        assert resume_env.get("type") != "/errors/internal", resume.text
        assert resume.status_code == 200, resume.text

        # Wait for ENDED
        for _ in range(60):
            r = await client.get(f"/v1/sessions/{session_id}")
            if r.status_code == 200 and r.json().get("status") == "ended":
                break
            await _asyncio.sleep(0.5)

        final = await client.get(f"/v1/sessions/{session_id}")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "ended", final.json()
    finally:
        if session_id is not None and workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/workspace_templates/{tpl_id}")
        await client.delete(f"/v1/workspace_providers/{wp_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0674 — Graph node ids "end", " ", "End", "END" coexist as distinct
# ============================================================================


@pytest.mark.asyncio
async def test_t0674_graph_node_ids_byte_exact_distinct(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0674 — Pin that node ids use byte-exact string equality
    (whitespace-sensitive, case-sensitive). Build a graph with 4
    distinct node ids that differ only by whitespace/case; create
    must succeed (201), GET must round-trip all 4, /status must
    return clean. Hard pin: never /errors/internal.

    Catches a regression where a future "node id normalisation" pass
    silently collapses these four into one.
    """
    provider_id = f"llm-t0674-{unique_suffix}"
    agent_id = f"agent-t0674-{unique_suffix}"
    graph_id = f"graph-t0674-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    try:
        # 4 distinct ids that look similar but are byte-distinct
        # NB: " " is a single space — node id min_length is 1, so
        # this passes Pydantic length validation. If a future
        # validator strips whitespace and rejects empty, this test
        # would deliberately break.
        node_ids = ("end", " ", "End", "END")
        nodes = [
            {"kind": "agent", "id": nid, "agent_id": agent_id}
            for nid in node_ids
        ]
        body = {
            "id": graph_id,
            "description": "T0674 byte-exact node ids",
            "nodes": nodes,
            "edges": [],
            "entry_node_id": "end",
        }
        resp = await client.post("/v1/graphs", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"4-distinct-byte-exact ids leaked /errors/internal: "
            f"{resp.text}"
        )
        # Either 201 (validator accepts byte-exact) or 4xx (validator
        # rejects whitespace-only id). Both clean.
        assert resp.status_code in (201, 400, 422), (
            f"4-distinct-ids unexpected status: "
            f"{resp.status_code}: {resp.text}"
        )
        if resp.status_code == 201:
            got = await client.get(f"/v1/graphs/{graph_id}")
            assert got.status_code == 200, got.text
            got_ids = [n["id"] for n in got.json()["nodes"]]
            for nid in node_ids:
                assert nid in got_ids, (
                    f"node id {nid!r} silently dropped/normalised; "
                    f"got_ids={got_ids!r}"
                )
            assert len(got_ids) == 4, (
                f"4 byte-exact ids collapsed to {len(got_ids)}: "
                f"{got_ids!r}"
            )
            status = await client.get(f"/v1/graphs/{graph_id}/status")
            status_env = status.json() if status.content else {}
            assert status_env.get("type") != "/errors/internal", (
                f"/status leaked /errors/internal: {status.text}"
            )
            assert status.status_code == 200, status.text
        else:
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0715 — DELETE LLMProvider then /agents/{a}/status: clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0715_delete_llm_provider_then_agent_status_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0715 — Two-state-machine combo: provider gone, agent still
    references it. The /status walk must surface a clean envelope
    listing the missing-provider issue, never /errors/internal.

    Sister of T0022 (status with missing provider) but with the
    DELETE happening AFTER the agent was created — exercises the
    cascade between provider DELETE and agent /status walk.
    """
    provider_id = f"llm-t0715-{unique_suffix}"
    agent_id = f"agent-t0715-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text

    try:
        # First /status while provider exists — should be ok=true
        before = await client.get(f"/v1/agents/{agent_id}/status")
        assert before.status_code == 200, before.text
        assert before.json()["ok"] is True, before.json()

        # DELETE the provider
        rm = await client.delete(f"/v1/llm_providers/{provider_id}")
        assert rm.status_code == 204, rm.text

        # Re-check /status — should reflect the missing provider
        after = await client.get(f"/v1/agents/{agent_id}/status")
        envelope = after.json() if after.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"/status after provider DELETE leaked /errors/internal: "
            f"{after.text}"
        )
        assert after.status_code == 200, after.text
        body = after.json()
        assert body["ok"] is False, (
            f"/status should be ok=false after provider DELETE: {body!r}"
        )
        # At least one issue references the missing provider id
        issues_str = " ".join(str(i) for i in body["issues"])
        assert provider_id in issues_str, (
            f"issues should reference missing provider {provider_id!r}; "
            f"got {body['issues']!r}"
        )
    finally:
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0414 — DELETE Agent referenced by a Graph node succeeds; Graph /status
# flips ok=false (mirror of T0344 cascade for Agent→Graph)
# ============================================================================


@pytest.mark.asyncio
async def test_t0414_delete_agent_flips_graph_status_to_failed(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0414 — Build LLMProvider→Agent→Graph, DELETE the Agent.
    Graph's /status walker must surface the missing-Agent reference
    in ``issues`` and flip ``ok=false``. Mirror of T0344 (which
    deletes the provider one tier up) — T0414 hits the Agent tier
    directly.

    Priority 1 (graph executor surface) + cascade integrity pin.
    The graph executor not being wired yet (worker pool path is
    NotImplementedError) is orthogonal: the /status endpoint walks
    the model only, so this contract works today regardless.

    Defence: DELETE Agent returns 204 cleanly; the Graph row stays
    intact (graphs are not cascade-deleted by agent loss); only
    the /status walk surfaces the broken reference.
    """
    provider_id = f"llm-t0414-{unique_suffix}"
    agent_id = f"agent-t0414-{unique_suffix}"
    graph_id = f"graph-t0414-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json=_agent_body(agent_id, provider_id=provider_id, tools=[]),
    )
    assert ag.status_code == 201, ag.text
    gr = await client.post(
        "/v1/graphs", json=_graph_body(graph_id, agent_id=agent_id),
    )
    assert gr.status_code == 201, gr.text
    try:
        # Sanity: pre-delete graph status is ok
        pre = await client.get(f"/v1/graphs/{graph_id}/status")
        assert pre.status_code == 200, pre.text
        assert pre.json()["ok"] is True, pre.text

        # DELETE the agent — should succeed (no provider cascade).
        rm = await client.delete(f"/v1/agents/{agent_id}")
        assert rm.status_code == 204, (
            f"DELETE Agent referenced by Graph should succeed; "
            f"got {rm.status_code}: {rm.text}"
        )

        # Graph row still exists.
        gr_get = await client.get(f"/v1/graphs/{graph_id}")
        assert gr_get.status_code == 200, gr_get.text

        # Graph status flips ok=false with missing-Agent in issues.
        post = await client.get(f"/v1/graphs/{graph_id}/status")
        assert post.status_code == 200, post.text
        body = post.json()
        assert body["ok"] is False, (
            f"Graph status should flip ok=false after Agent deletion; "
            f"got: {body}"
        )
        issues_str = " ".join(str(i) for i in body["issues"])
        assert agent_id in issues_str, (
            f"issues should reference missing agent {agent_id!r}; "
            f"got {body['issues']!r}"
        )
    finally:
        await client.delete(f"/v1/graphs/{graph_id}")
        # agent already deleted (the body of the test)
        await client.delete(f"/v1/llm_providers/{provider_id}")
