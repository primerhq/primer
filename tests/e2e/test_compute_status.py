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
