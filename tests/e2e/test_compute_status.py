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
