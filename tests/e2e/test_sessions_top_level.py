"""E2E: top-level cross-workspace Sessions surface + lifecycle edges.

Covers backlog items T0038 (resume idempotency), T0039 (cancel from
CREATED returns terminal row), T0040 (filter by workspace_id),
T0041 (filter intersection agent_id + status), T0042 (top-level GET),
T0089 (top-level ordering by created_at).

Setup chain in every test: LLMProvider → Agent → WorkspaceProvider →
WorkspaceTemplate → Workspace(s) → Session(s) (binding=agent, no
auto_start). Sessions stay in `CREATED` status without auto_start, so
the worker pool never has to actually process a turn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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


def _agent_body(entity_id: str, *, provider_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "test agent",
        "model": {"provider_id": provider_id, "model_name": "claude-sonnet-4-6"},
        "tools": [],
    }


def _workspace_provider_body(entity_id: str, root: Path) -> dict:
    return {
        "id": entity_id,
        "provider": "local",
        "config": {"kind": "local", "path": str(root)},
    }


def _workspace_template_body(entity_id: str, *, provider_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "test template",
        "provider_id": provider_id,
        "backend": {"kind": "local"},
    }


def _session_body(*, agent_id: str) -> dict:
    return {
        "binding": {"kind": "agent", "agent_id": agent_id},
        "auto_start": False,
    }


@pytest.mark.asyncio
async def test_t0042_top_level_get_session_works_without_workspace_context(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    provider_id = f"llm-{unique_suffix}"
    agent_id = f"agent-{unique_suffix}"
    wp_id = f"wp-{unique_suffix}"
    tpl_id = f"wt-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    try:
        ag = await client.post(
            "/v1/agents", json=_agent_body(agent_id, provider_id=provider_id),
        )
        assert ag.status_code == 201, ag.text
        try:
            wp = await client.post(
                "/v1/workspace_providers",
                json=_workspace_provider_body(wp_id, tmp_path),
            )
            assert wp.status_code == 201, wp.text
            try:
                tpl = await client.post(
                    "/v1/workspace_templates",
                    json=_workspace_template_body(tpl_id, provider_id=wp_id),
                )
                assert tpl.status_code == 201, tpl.text
                try:
                    ws = await client.post(
                        "/v1/workspaces",
                        json={"template_id": tpl_id},
                    )
                    assert ws.status_code == 201, ws.text
                    workspace_id = ws.json()["id"]

                    sess = await client.post(
                        f"/v1/workspaces/{workspace_id}/sessions",
                        json=_session_body(agent_id=agent_id),
                    )
                    assert sess.status_code == 201, sess.text
                    session_row = sess.json()
                    session_id = session_row["id"]
                    assert session_row["workspace_id"] == workspace_id

                    # Top-level GET — no workspace prefix in the URL
                    top = await client.get(f"/v1/sessions/{session_id}")
                    assert top.status_code == 200, top.text
                    top_body = top.json()
                    assert top_body["id"] == session_id
                    assert top_body["workspace_id"] == workspace_id
                    # Binding identity is preserved
                    assert top_body["binding"]["kind"] == "agent"
                    assert top_body["binding"]["agent_id"] == agent_id

                    # Top-level lookup of a missing id is a clean 404
                    missing = await client.get(
                        f"/v1/sessions/missing-{unique_suffix}",
                    )
                    assert missing.status_code == 404, missing.text
                    assert missing.json()["type"] == "/errors/not-found"
                finally:
                    if workspace_id is not None:
                        await client.delete(f"/v1/workspaces/{workspace_id}")
                    await client.delete(f"/v1/workspace_templates/{tpl_id}")
            finally:
                await client.delete(f"/v1/workspace_providers/{wp_id}")
        finally:
            await client.delete(f"/v1/agents/{agent_id}")
    finally:
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# Shared setup helper for the lifecycle/filter tests below
# ============================================================================


async def _full_setup(
    client: httpx.AsyncClient, suffix: str, tmp_path: Path,
) -> dict:
    """Materialise provider+agent+workspace_provider+template+workspace,
    return all ids in a dict for the test to use + tear down."""
    provider_id = f"llm-{suffix}"
    agent_id = f"agent-{suffix}"
    wp_id = f"wp-{suffix}"
    tpl_id = f"wt-{suffix}"

    pr = await client.post("/v1/llm_providers", json=_llm_body(provider_id))
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents", json=_agent_body(agent_id, provider_id=provider_id),
    )
    assert ag.status_code == 201, ag.text
    wp = await client.post(
        "/v1/workspace_providers",
        json=_workspace_provider_body(wp_id, tmp_path),
    )
    assert wp.status_code == 201, wp.text
    tpl = await client.post(
        "/v1/workspace_templates",
        json=_workspace_template_body(tpl_id, provider_id=wp_id),
    )
    assert tpl.status_code == 201, tpl.text
    return {
        "provider_id": provider_id,
        "agent_id": agent_id,
        "wp_id": wp_id,
        "tpl_id": tpl_id,
    }


async def _teardown_setup(client: httpx.AsyncClient, env: dict) -> None:
    await client.delete(f"/v1/workspace_templates/{env['tpl_id']}")
    await client.delete(f"/v1/workspace_providers/{env['wp_id']}")
    await client.delete(f"/v1/agents/{env['agent_id']}")
    await client.delete(f"/v1/llm_providers/{env['provider_id']}")


async def _create_workspace_and_session(
    client: httpx.AsyncClient, *, tpl_id: str, agent_id: str,
) -> tuple[str, str]:
    """Materialise one workspace + one CREATED session on it."""
    ws = await client.post("/v1/workspaces", json={"template_id": tpl_id})
    assert ws.status_code == 201, ws.text
    workspace_id = ws.json()["id"]
    sess = await client.post(
        f"/v1/workspaces/{workspace_id}/sessions",
        json=_session_body(agent_id=agent_id),
    )
    assert sess.status_code == 201, sess.text
    return workspace_id, sess.json()["id"]


# ============================================================================
# T0038 — resume is idempotent (twice in a row both return 200)
# ============================================================================


@pytest.mark.asyncio
async def test_t0038_session_resume_is_idempotent(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        first = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        assert first.status_code == 200, first.text
        assert first.json()["id"] == session_id
        # The CREATED → RUNNING transition is observable here.
        assert first.json()["status"] == "running", first.json()

        # Second resume is a documented idempotent no-op (spec §13;
        # handler returns the row unchanged).
        second = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        assert second.status_code == 200, second.text
        assert second.json()["id"] == session_id
        assert second.json()["status"] == "running", second.json()
    finally:
        if workspace_id is not None:
            # cancel the session to release any worker lease before
            # tearing down the workspace
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0039 — cancel from CREATED returns the updated row in a terminal state
# ============================================================================


@pytest.mark.asyncio
async def test_t0039_session_cancel_returns_terminal_row(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        body = cancel.json()
        # CREATED → ENDED directly (no worker holding a lease).
        assert body["id"] == session_id
        assert body["status"] == "ended", body
        assert body["ended_reason"] == "cancelled", body
        assert body.get("ended_at") is not None, body

        # Idempotency negative case: second cancel on an ENDED session
        # is a 409 (per the handler's explicit ConflictError).
        again = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert again.status_code == 409, again.text
        assert again.json()["type"] == "/errors/conflict"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0040 — top-level GET /v1/sessions?workspace_id=<id> filters correctly
# ============================================================================


@pytest.mark.asyncio
async def test_t0040_top_level_sessions_filter_by_workspace_id(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    env = await _full_setup(client, unique_suffix, tmp_path)
    wid_a: str | None = None
    wid_b: str | None = None
    try:
        # Two workspaces from the same template, one session each.
        wid_a, sid_a = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )
        wid_b, sid_b = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        resp = await client.get(
            "/v1/sessions",
            params={"workspace_id": wid_a, "limit": 50, "offset": 0},
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        ids = [item["id"] for item in items]
        # Filter intersection: only sessions on workspace A.
        assert sid_a in ids, f"missing {sid_a!r} in A-filtered: {ids!r}"
        assert sid_b not in ids, f"unrelated {sid_b!r} leaked into A: {ids!r}"
        # All returned items must in fact reference workspace A.
        for item in items:
            assert item["workspace_id"] == wid_a, item
    finally:
        for wid, sid in ((wid_a, locals().get("sid_a")), (wid_b, locals().get("sid_b"))):
            if wid is not None:
                await client.delete(f"/v1/workspaces/{wid}")
        await _teardown_setup(client, env)


# ============================================================================
# T0041 — filter intersection: agent_id + status combine via AND
# ============================================================================


@pytest.mark.asyncio
async def test_t0041_top_level_sessions_filter_intersects_agent_id_and_status(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        # Two sessions on the same workspace: one stays CREATED, the
        # other is cancelled → ENDED. Filtering by agent + status
        # should pick exactly one.
        workspace_id, sid_created = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )
        sess2 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json=_session_body(agent_id=env["agent_id"]),
        )
        assert sess2.status_code == 201, sess2.text
        sid_ended = sess2.json()["id"]
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{sid_ended}/cancel",
        )
        assert cancel.status_code == 200, cancel.text

        # Filter: agent_id=<agent> AND status=created → exactly sid_created
        resp = await client.get(
            "/v1/sessions",
            params={
                "agent_id": env["agent_id"],
                "status": "created",
                "limit": 50,
                "offset": 0,
            },
        )
        assert resp.status_code == 200, resp.text
        ids = [item["id"] for item in resp.json()["items"]]
        assert sid_created in ids, ids
        assert sid_ended not in ids, ids
        # Cross-check the opposite filter
        resp2 = await client.get(
            "/v1/sessions",
            params={
                "agent_id": env["agent_id"],
                "status": "ended",
                "limit": 50,
                "offset": 0,
            },
        )
        assert resp2.status_code == 200, resp2.text
        ids2 = [item["id"] for item in resp2.json()["items"]]
        assert sid_ended in ids2, ids2
        assert sid_created not in ids2, ids2
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0089 — top-level /v1/sessions order_by created_at asc/desc are reverses
# ============================================================================


@pytest.mark.asyncio
async def test_t0089_top_level_sessions_order_by_created_at_reverses(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0089 — `GET /v1/sessions?order_by=created_at:asc` and
    `?order_by=created_at:desc` over the same set return the
    sessions in reversed order. Pin the basic ordering invariant
    on the cross-workspace surface.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, _ = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )
        # Create 2 more sessions with brief gaps so created_at differs.
        # asyncio.sleep(0.05) is plenty — Postgres timestamptz has
        # microsecond resolution.
        await asyncio.sleep(0.05)
        s2 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json=_session_body(agent_id=env["agent_id"]),
        )
        assert s2.status_code == 201, s2.text
        await asyncio.sleep(0.05)
        s3 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json=_session_body(agent_id=env["agent_id"]),
        )
        assert s3.status_code == 201, s3.text

        # Filter by workspace_id so the assertion is over only OUR sessions
        async def _walk(direction: str) -> list[str]:
            r = await client.get(
                "/v1/sessions",
                params={
                    "workspace_id": workspace_id,
                    "order_by": f"created_at:{direction}",
                    "limit": 50,
                    "offset": 0,
                },
            )
            assert r.status_code == 200, r.text
            return [item["id"] for item in r.json()["items"]]

        ascending = await _walk("asc")
        descending = await _walk("desc")
        assert sorted(ascending) == sorted(descending), (
            f"asc/desc returned different sets: {ascending!r} vs {descending!r}"
        )
        assert descending == list(reversed(ascending)), (
            f"desc {descending!r} is not the reverse of asc {ascending!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)
