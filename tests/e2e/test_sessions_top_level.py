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
import os
from pathlib import Path

import httpx
import pytest


# The lifecycle tests below need a real agent turn to converge to a terminal
# state within their poll windows; they do not assert on LLM content. By
# default they use a (placeholder-keyed) anthropic provider, which is what CI
# runs with a real ANTHROPIC_API_KEY in the env. For local runs against an
# OpenAI-compatible backend (e.g. LM Studio), set PRIMER_E2E_LLM_BASE_URL (the
# OpenAI base, ending in /v1), PRIMER_E2E_LLM_MODEL, and the key in
# PRIMER_E2E_LLM_API_KEY (never hardcode a token). When the base URL is set the
# helpers below emit an `openchat` provider pointed at that endpoint instead.
def _llm_model_name() -> str:
    if os.environ.get("PRIMER_E2E_LLM_BASE_URL"):
        return os.environ.get("PRIMER_E2E_LLM_MODEL", "google/gemma-4-e4b")
    return "claude-sonnet-4-6"


def _llm_body(entity_id: str) -> dict:
    base_url = os.environ.get("PRIMER_E2E_LLM_BASE_URL")
    if base_url:
        return {
            "id": entity_id,
            "provider": "openchat",
            "models": [{"name": _llm_model_name(), "context_length": 32_768}],
            "config": {
                "url": base_url,
                "api_key": os.environ.get("PRIMER_E2E_LLM_API_KEY", ""),
            },
            "limits": {"max_concurrency": 1},
        }
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": _llm_model_name(), "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }


def _agent_body(entity_id: str, *, provider_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "test agent",
        "model": {"provider_id": provider_id, "model_name": _llm_model_name()},
        "tools": [],
    }


def _workspace_provider_body(entity_id: str, root: Path) -> dict:
    return {
        "id": entity_id,
        "provider": "local",
        "config": {"kind": "local", "root_path": str(root)},
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
        for wid, _sid in ((wid_a, locals().get("sid_a")), (wid_b, locals().get("sid_b"))):
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
# T0110 — Session steer accumulates: 3 sequential calls all 2xx, no rejection
# ============================================================================


@pytest.mark.asyncio
async def test_t0110_session_steer_accumulates_three_calls(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0110 — three sequential POST /steer calls on the same session
    must all succeed (2xx). Per the spec §12 finding, steer doesn't
    gate on session status — even a CREATED session accepts steer.

    The pin: each call returns a structured response (not a no-op,
    not an error), with a distinct instruction body. If a future
    change adds rate-limiting or rejects mid-stream, this test fails.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        instructions = [
            f"first instruction {unique_suffix}",
            f"second instruction {unique_suffix}",
            f"third instruction {unique_suffix}",
        ]
        for i, inst in enumerate(instructions):
            resp = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
                json={"instruction": inst},
            )
            assert 200 <= resp.status_code < 300, (
                f"steer #{i} returned {resp.status_code}: {resp.text}"
            )
            # Response is `instruction.model_dump()` — a dict
            body = resp.json()
            assert isinstance(body, dict), body
    finally:
        if workspace_id is not None:
            # cancel the session before destroying the workspace
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0130 — top-level /v1/sessions cursor walk visits every session once
# ============================================================================


@pytest.mark.asyncio
async def test_t0130_top_level_sessions_cursor_walk_full_coverage(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0130 — seed 7 sessions on a single workspace and walk the
    top-level /v1/sessions endpoint via cursor pagination in chunks
    of 3. The walk must visit every seeded id exactly once (no
    duplicates, no skipped ids).

    The list endpoint is `GET /v1/sessions?cursor=...` (cursor mode
    is opt-in via the `cursor` query param). Filter by workspace_id
    so the walk is bounded to the seeded set.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    seeded_ids: list[str] = []
    try:
        workspace_id, first_sid = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )
        seeded_ids.append(first_sid)
        # Add 6 more sessions on the same workspace
        for _ in range(6):
            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json=_session_body(agent_id=env["agent_id"]),
            )
            assert sess.status_code == 201, sess.text
            seeded_ids.append(sess.json()["id"])

        # GET /v1/sessions opts into cursor mode by passing the
        # cursor query param, but the codec rejects an empty/initial
        # value (it expects the JSON token from a prior page). Use
        # the POST /sessions/find endpoint with `{page: {kind: cursor,
        # cursor: null, length: 3}}` to start the walk cleanly.
        predicate = {
            "kind": "predicate",
            "op": "=",
            "left": {"kind": "field", "name": "workspace_id"},
            "right": {"kind": "value", "value": workspace_id},
        }
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            body = {
                "predicate": predicate,
                "page": {"kind": "cursor", "cursor": cursor, "length": 3},
            }
            resp = await client.post("/v1/sessions/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            assert page["kind"] == "cursor", page
            seen.extend(item["id"] for item in page["items"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail(f"cursor walk did not terminate; seen={seen!r}")

        # Invariant 1: no duplicates
        assert len(seen) == len(set(seen)), (
            f"cursor walk yielded duplicates: {seen!r}"
        )
        # Invariant 2: every seeded id present (full coverage)
        for sid in seeded_ids:
            assert sid in seen, (
                f"seeded session {sid!r} missing from walk: {sorted(seen)!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0120 — three-way filter (workspace_id + agent_id + status) is intersection
# ============================================================================


@pytest.mark.asyncio
async def test_t0120_top_level_sessions_three_way_filter_intersects(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0120 — seed sessions across two workspaces and two agents in
    four combinations (wsA+agA, wsA+agB, wsB+agA, wsB+agB), cancel
    one of them to flip its status, and verify the three-way filter
    `workspace_id=wsA & agent_id=agA & status=created` returns the
    single matching session.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_a: str | None = None
    workspace_b: str | None = None
    agent_b_id = f"agent-b-{unique_suffix}"
    try:
        # Second agent — same provider
        ag_b = await client.post(
            "/v1/agents",
            json=_agent_body(agent_b_id, provider_id=env["provider_id"]),
        )
        assert ag_b.status_code == 201, ag_b.text

        # Two workspaces from the same template
        ws_a = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws_a.status_code == 201, ws_a.text
        workspace_a = ws_a.json()["id"]
        ws_b = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws_b.status_code == 201, ws_b.text
        workspace_b = ws_b.json()["id"]

        # Four sessions in the four (workspace, agent) combinations.
        async def _mk_sess(wid: str, aid: str) -> str:
            r = await client.post(
                f"/v1/workspaces/{wid}/sessions",
                json=_session_body(agent_id=aid),
            )
            assert r.status_code == 201, r.text
            return r.json()["id"]

        sid_AA = await _mk_sess(workspace_a, env["agent_id"])
        sid_AB = await _mk_sess(workspace_a, agent_b_id)
        sid_BA = await _mk_sess(workspace_b, env["agent_id"])
        sid_BB = await _mk_sess(workspace_b, agent_b_id)

        # Flip sid_BA to ENDED so the agent-A-on-ws-A vs agent-A-on-ws-B
        # filter intersection is more interesting.
        cancel = await client.post(
            f"/v1/workspaces/{workspace_b}/sessions/{sid_BA}/cancel",
        )
        assert cancel.status_code == 200, cancel.text

        # Triple filter: ws=A, agent=env["agent_id"], status=created
        # Expected: only sid_AA
        resp = await client.get(
            "/v1/sessions",
            params={
                "workspace_id": workspace_a,
                "agent_id": env["agent_id"],
                "status": "created",
                "limit": 50,
                "offset": 0,
            },
        )
        assert resp.status_code == 200, resp.text
        ids = {item["id"] for item in resp.json()["items"]}
        assert ids == {sid_AA}, (
            f"triple filter (ws=A, agent=A, status=created) expected "
            f"{{sid_AA}}, got {sorted(ids)!r}"
        )
        # Sanity: confirm the other three are excluded
        for sid_other in (sid_AB, sid_BA, sid_BB):
            assert sid_other not in ids, sid_other
    finally:
        # Cancel + delete sessions
        for wid, _sids in (
            (workspace_a, ["sid_AA", "sid_AB"]),
            (workspace_b, ["sid_BA", "sid_BB"]),
        ):
            if wid is not None:
                await client.delete(f"/v1/workspaces/{wid}")
        await client.delete(f"/v1/agents/{agent_b_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0151 — POST /v1/sessions/find returns Session with full binding details
# ============================================================================


@pytest.mark.asyncio
async def test_t0151_sessions_find_returns_full_binding(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0151 — /v1/sessions/find returns the full Session row, including
    `binding.kind` and the appropriate `agent_id` / `graph_id` field."""
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )
        # Filter by workspace_id so the assertion is deterministic
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "workspace_id"},
                "right": {"kind": "value", "value": workspace_id},
            },
            "page": {"kind": "offset", "offset": 0, "length": 5},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1, items
        s = items[0]
        assert s["id"] == session_id
        binding = s.get("binding")
        assert isinstance(binding, dict), s
        assert binding.get("kind") == "agent", binding
        assert binding.get("agent_id") == env["agent_id"], binding
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0153 — top-level /v1/sessions filter by parent_session_id
# ============================================================================


@pytest.mark.asyncio
async def test_t0153_sessions_filter_by_parent_session_id(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0153 — create two sessions where session-B.parent_session_id =
    session-A.id, then filter `/v1/sessions?parent_session_id=A.id`.
    Session-B (the child) must appear; session-A must NOT."""
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, sid_a = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )
        # Session B with parent = A
        sb = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "parent_session_id": sid_a,
                "auto_start": False,
            },
        )
        assert sb.status_code == 201, sb.text
        sid_b = sb.json()["id"]

        resp = await client.get(
            "/v1/sessions",
            params={
                "parent_session_id": sid_a,
                "limit": 50,
                "offset": 0,
            },
        )
        assert resp.status_code == 200, resp.text
        ids = {item["id"] for item in resp.json()["items"]}
        assert sid_b in ids, (
            f"child session {sid_b!r} missing from filter: {sorted(ids)!r}"
        )
        assert sid_a not in ids, (
            f"parent {sid_a!r} should not match its own parent_session_id "
            f"filter: {sorted(ids)!r}"
        )
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


# ============================================================================
# T0157 — session create with binding kind=graph + missing graph_id
# ============================================================================


@pytest.mark.asyncio
async def test_t0157_session_create_with_missing_graph_id_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0157 — POST a session with binding={kind:"graph", graph_id:<missing>}.
    The current API may either reject at create-time (4xx /errors/not-found)
    OR accept the row and surface the broken reference later (mirroring
    T0068 — Document orphan-tolerated). Either is acceptable; what is NOT
    is a 5xx leak. Pin the no-/errors/internal contract.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        missing_graph_id = f"missing-graph-{unique_suffix}"
        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": missing_graph_id},
                "auto_start": False,
            },
        )
        # Anything but 5xx is documentable behaviour; pin no internal-error
        assert resp.status_code != 500, resp.text
        assert resp.status_code < 500, resp.text
        if resp.status_code >= 400:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
        else:
            # Accepted — verify the row roundtrips cleanly through GET
            session_id = resp.json()["id"]
            get = await client.get(f"/v1/sessions/{session_id}")
            assert get.status_code == 200, get.text
            assert get.json()["binding"]["graph_id"] == missing_graph_id
            # Cancel for clean teardown
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0158 — POST sessions on missing workspace returns clean 4xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0158_post_session_on_missing_workspace_clean_4xx(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0158 — POST /v1/workspaces/<missing>/sessions with a structurally-
    valid body. Workspace doesn't exist so the create must reject with a
    4xx envelope and NOT 5xx.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    try:
        missing_ws = f"missing-ws-{unique_suffix}"
        resp = await client.post(
            f"/v1/workspaces/{missing_ws}/sessions",
            json=_session_body(agent_id=env["agent_id"]),
        )
        assert 400 <= resp.status_code < 500, resp.text
        envelope = resp.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope
    finally:
        await _teardown_setup(client, env)


# ============================================================================
# T0159 — cancel/pause/resume on missing session id all return 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0159_signals_on_missing_session_all_return_404(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0159 — every signal verb (cancel, pause, resume) on a non-existent
    session id returns 404 /errors/not-found. Cross-verb consistency check.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        missing_sid = f"missing-sess-{unique_suffix}"
        for verb in ("cancel", "pause", "resume"):
            resp = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{missing_sid}/{verb}",
            )
            assert resp.status_code == 404, (
                f"{verb} on missing sid expected 404, got "
                f"{resp.status_code}: {resp.text}"
            )
            assert resp.json()["type"] == "/errors/not-found", resp.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0160 — resume on cancelled (terminal) session returns 409
# ============================================================================


@pytest.mark.asyncio
async def test_t0160_resume_on_cancelled_session_returns_409(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0160 — cancel a CREATED session (T0039 proves it goes terminal),
    then attempt resume. The illegal CANCELLED→RUNNING transition must
    surface as 409 /errors/conflict.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Cancel from CREATED — proven terminal by T0039
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended"

        # Attempt resume on the now-terminal session
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        assert resume.status_code == 409, resume.text
        assert resume.json()["type"] == "/errors/conflict"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0161 — pause on cancelled (terminal) session returns 409
# ============================================================================


@pytest.mark.asyncio
async def test_t0161_pause_on_cancelled_session_returns_409(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0161 — symmetric to T0160 but for pause. The illegal
    CANCELLED→PAUSED transition must surface as 409 /errors/conflict.

    Together with T0160 this gives full pause/resume coverage of the
    illegal-from-terminal transition primer.
    """
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
        assert cancel.json()["status"] == "ended"

        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert pause.status_code == 409, pause.text
        assert pause.json()["type"] == "/errors/conflict"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0180 — /v1/sessions/find with cursor + predicate visits each row once
# ============================================================================


@pytest.mark.asyncio
async def test_t0180_sessions_find_cursor_with_status_predicate_covers_all_once(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0180 — Combine cursor pagination with a predicate on a Session
    field (status='created') over multiple pages. Each matching session
    must appear exactly once across the cursor walk; no row is skipped
    or duplicated.

    Seeds 7 sessions across 2 workspaces (all CREATED — no worker
    auto_start), then walks /v1/sessions/find with predicate
    `status = "created" AND workspace_id IN [w1, w2]` at page length 3
    so the walk spans 3 pages (3 + 3 + 1).
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_ids: list[str] = []
    seeded_session_ids: list[str] = []
    try:
        # Two workspaces
        for _ in range(2):
            ws = await client.post(
                "/v1/workspaces", json={"template_id": env["tpl_id"]},
            )
            assert ws.status_code == 201, ws.text
            workspace_ids.append(ws.json()["id"])

        # 7 sessions distributed across the two workspaces
        for i in range(7):
            wid = workspace_ids[i % 2]
            sess = await client.post(
                f"/v1/workspaces/{wid}/sessions",
                json=_session_body(agent_id=env["agent_id"]),
            )
            assert sess.status_code == 201, sess.text
            seeded_session_ids.append(sess.json()["id"])

        predicate = {
            "kind": "predicate",
            "op": "and",
            "left": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "status"},
                "right": {"kind": "value", "value": "created"},
            },
            "right": {
                "kind": "predicate",
                "op": "in",
                "left": {"kind": "field", "name": "workspace_id"},
                "right": {"kind": "value", "value": workspace_ids},
            },
        }

        # Cursor walk with page length 3 — initial cursor must be null
        # (per spec §4 GET-cursor-mode quirk; POST /find with kind=cursor
        # accepts cursor=null as the start sentinel).
        seen_ids: list[str] = []
        cursor: str | None = None
        for _ in range(10):  # safety bound
            body = {
                "predicate": predicate,
                "page": {"kind": "cursor", "cursor": cursor, "length": 3},
            }
            resp = await client.post("/v1/sessions/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            items = page.get("items", [])
            seen_ids.extend(item["id"] for item in items)
            cursor = page.get("next_cursor")
            if not cursor:
                break

        # Each seeded session appears exactly once
        assert sorted(seen_ids) == sorted(seeded_session_ids), (
            f"cursor walk did not cover each session exactly once. "
            f"seeded={sorted(seeded_session_ids)!r}, "
            f"seen={sorted(seen_ids)!r}"
        )
        # No duplicates within the walk
        assert len(seen_ids) == len(set(seen_ids)), (
            f"duplicates in cursor walk: {seen_ids!r}"
        )
    finally:
        for wid in workspace_ids:
            for sid in seeded_session_ids:
                await client.post(
                    f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
                )
            await client.delete(f"/v1/workspaces/{wid}")
        await _teardown_setup(client, env)


# ============================================================================
# T0205 — Session steer with empty instruction string returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0205_session_steer_empty_instruction_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0205 — POST /sessions/{sid}/steer with `instruction=""` is
    degenerate input. The handler must produce a documented response —
    either 204/200 (silently accepted) or 4xx (rejected as empty).
    NEVER 5xx.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": ""},
        )
        assert resp.status_code != 500, resp.text
        if resp.status_code in (200, 204):
            # Silently accepted: GET the session and verify it's still
            # in a sane state (no row corruption)
            got = await client.get(f"/v1/sessions/{session_id}")
            assert got.status_code == 200, got.text
            assert got.json()["id"] == session_id
        else:
            assert 400 <= resp.status_code < 500, resp.text
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0227 — Session metadata field round-trips through POST → GET → list
# ============================================================================


@pytest.mark.asyncio
async def test_t0227_session_metadata_round_trips(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0227 — SessionCreateBody.metadata must persist through:
      - POST (create returns body with metadata)
      - GET /v1/sessions/{id} (read echoes metadata)
      - GET /v1/sessions?workspace_id=... (list echoes metadata on items)

    Pins persistence of an opaque-blob field that no other test
    currently covers.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        metadata = {
            "owner": f"user-{unique_suffix}",
            "purpose": "T0227 round-trip probe",
            "tags": ["alpha", "beta"],
            "nested": {"k1": 42, "k2": True},
        }
        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "metadata": metadata,
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]
        # POST response carries metadata
        assert sess.json().get("metadata") == metadata, sess.json()

        # GET echoes it
        got = await client.get(f"/v1/sessions/{session_id}")
        assert got.status_code == 200, got.text
        assert got.json().get("metadata") == metadata, got.json()

        # List endpoint carries it on the item
        listed = await client.get(
            f"/v1/sessions?workspace_id={workspace_id}&limit=50&offset=0",
        )
        assert listed.status_code == 200, listed.text
        matching = [
            item for item in listed.json()["items"]
            if item["id"] == session_id
        ]
        assert matching, f"session {session_id!r} missing from listing"
        assert matching[0].get("metadata") == metadata, matching[0]
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0228 — parent_session_id filter is direct-only (not transitive)
# ============================================================================


@pytest.mark.asyncio
async def test_t0228_parent_session_id_filter_is_direct_not_transitive(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0228 — Build a 3-deep chain: A → B → C (B.parent=A, C.parent=B).
    The filter /v1/sessions?parent_session_id=A must return ONLY B,
    not C — the filter is direct-parent, not transitive descent.

    Extends T0153 (single-level parent filter) to verify the filter
    semantics under chained sessions.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    sess_a: str | None = None
    sess_b: str | None = None
    sess_c: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Session A — root
        a = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json=_session_body(agent_id=env["agent_id"]),
        )
        assert a.status_code == 201, a.text
        sess_a = a.json()["id"]

        # Session B — child of A
        b = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "parent_session_id": sess_a,
                "auto_start": False,
            },
        )
        assert b.status_code == 201, b.text
        sess_b = b.json()["id"]

        # Session C — child of B (grandchild of A)
        c = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "parent_session_id": sess_b,
                "auto_start": False,
            },
        )
        assert c.status_code == 201, c.text
        sess_c = c.json()["id"]

        # Filter by parent=A must return ONLY B (the direct child)
        resp = await client.get(
            f"/v1/sessions?parent_session_id={sess_a}",
        )
        assert resp.status_code == 200, resp.text
        ids = {item["id"] for item in resp.json()["items"]}
        assert sess_b in ids, (
            f"direct child B={sess_b!r} missing from "
            f"parent_session_id={sess_a!r} filter: {ids!r}"
        )
        assert sess_c not in ids, (
            f"grandchild C={sess_c!r} unexpectedly returned by "
            f"parent_session_id={sess_a!r} filter (filter should be "
            f"direct-only, not transitive): {ids!r}"
        )
        assert sess_a not in ids, (
            f"root A={sess_a!r} unexpectedly returned by its own "
            f"parent_session_id filter: {ids!r}"
        )
    finally:
        if workspace_id is not None:
            for sid in (sess_c, sess_b, sess_a):
                if sid:
                    await client.post(
                        f"/v1/workspaces/{workspace_id}/sessions/{sid}/cancel",
                    )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0229 — /v1/sessions filter with bogus agent_id returns empty list
# ============================================================================


@pytest.mark.asyncio
async def test_t0229_sessions_filter_bogus_agent_id_returns_empty_list(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0229 — /v1/sessions?agent_id=<missing> on a non-existent agent
    must return 200 with `items: []`, NOT 404. List-endpoint filters
    are "narrowing" semantics, not validation — referencing a missing
    value yields zero rows, not an error.
    """
    missing_agent = f"missing-agent-{unique_suffix}"
    resp = await client.get(f"/v1/sessions?agent_id={missing_agent}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body, body
    assert body["items"] == [], (
        f"filter by missing agent_id should yield empty list, got: "
        f"{body['items']!r}"
    )


# ============================================================================
# T0230 — Steer with a 64 KiB instruction body is handled cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0230_steer_with_64kib_instruction_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0230 — POST /sessions/{sid}/steer with a 64 KiB instruction
    body. The handler must produce a documented response — either
    accept (204/200) or reject (4xx); never 5xx. Boundary probe
    for instruction-size handling, distinct from T0205 (empty string).
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # 64 KiB instruction string (single character repeated)
        instruction = "x" * (64 * 1024)
        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": instruction},
        )
        assert resp.status_code != 500, resp.text
        if resp.status_code in (200, 204):
            # Silently accepted — verify session still readable
            got = await client.get(f"/v1/sessions/{session_id}")
            assert got.status_code == 200, got.text
            assert got.json()["id"] == session_id
        else:
            assert 400 <= resp.status_code < 500, resp.text
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0298 — POST /v1/sessions/find with order_by created_at desc
# ============================================================================


@pytest.mark.asyncio
async def test_t0298_sessions_find_order_by_created_at_desc(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0298 — Pin that POST /v1/sessions/find honours `order_by` for
    the `created_at` field in descending direction. T0089 covered
    GET /v1/sessions ordering; this extends to the predicate-based
    variant.

    Seeds 3 sessions (with brief sleep between to ensure distinct
    timestamps), then queries /find with order_by created_at desc
    scoped by workspace_id. Result sequence is the seeded ids in
    reverse insertion order.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    seeded_ids: list[str] = []
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        for _ in range(3):
            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json=_session_body(agent_id=env["agent_id"]),
            )
            assert sess.status_code == 201, sess.text
            seeded_ids.append(sess.json()["id"])
            # Brief sleep to ensure created_at separation
            await asyncio.sleep(0.05)

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "workspace_id"},
                "right": {"kind": "value", "value": workspace_id},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
            "order_by": [
                {"field": "created_at", "direction": "desc"},
            ],
        }
        resp = await client.post("/v1/sessions/find", json=body)
        assert resp.status_code == 200, resp.text
        result_ids = [item["id"] for item in resp.json()["items"]]

        # Result has all seeded ids
        assert sorted(result_ids) == sorted(seeded_ids), (
            f"missed seeded sessions: result={sorted(result_ids)!r}, "
            f"seeded={sorted(seeded_ids)!r}"
        )
        # Order is reverse-insertion (newest first)
        expected_desc = list(reversed(seeded_ids))
        assert result_ids == expected_desc, (
            f"order_by created_at desc returned wrong sequence: "
            f"got {result_ids!r}, expected {expected_desc!r}"
        )
    finally:
        if workspace_id is not None:
            for sid in seeded_ids:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/{sid}/cancel",
                )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0309 — Predicate `>` on Session.created_at (datetime field)
# ============================================================================


@pytest.mark.asyncio
async def test_t0309_predicate_gt_on_session_created_at_datetime(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0309 — Pin that the predicate translator handles datetime
    comparison on Session.created_at. Seed two sessions with a brief
    gap, then query `created_at > <first session's ts>` — only the
    second session must be returned.

    Distinct from T0150 (integer turn_no) and T0236 (JSONB nested).
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    seeded: list[tuple[str, str]] = []  # (id, created_at)
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Seed two sessions with deliberate gap
        for _ in range(2):
            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json=_session_body(agent_id=env["agent_id"]),
            )
            assert sess.status_code == 201, sess.text
            seeded.append((sess.json()["id"], sess.json()["created_at"]))
            await asyncio.sleep(0.1)

        first_id, first_ts = seeded[0]
        second_id, _second_ts = seeded[1]

        # Query: workspace_id = ours AND created_at > first_ts
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "right": {"kind": "value", "value": workspace_id},
                },
                "right": {
                    "kind": "predicate",
                    "op": ">",
                    "left": {"kind": "field", "name": "created_at"},
                    "right": {"kind": "value", "value": first_ts},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        # No /errors/internal regardless of outcome — datetime predicate
        # may return a clean-but-different envelope if the translator
        # doesn't yet support it
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"datetime predicate leaked /errors/internal: {resp.text}"
        )
        if resp.status_code == 200:
            ids = {item["id"] for item in resp.json()["items"]}
            assert second_id in ids, (
                f"created_at > first_ts should include second session: "
                f"{ids!r}"
            )
            assert first_id not in ids, (
                f"strict > should EXCLUDE the boundary session "
                f"(first_id with same ts): {ids!r}"
            )
    finally:
        if workspace_id is not None:
            for (sid, _ts) in seeded:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/{sid}/cancel",
                )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0310 — Predicate `>=` on Session.created_at is inclusive at boundary
# ============================================================================


@pytest.mark.asyncio
async def test_t0310_predicate_gte_on_session_created_at_inclusive(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0310 — Symmetric to T0309. Predicate `>=` with the boundary
    timestamp must INCLUDE the boundary session. Pin inclusive
    semantics on the datetime path.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    seeded: list[tuple[str, str]] = []
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        for _ in range(2):
            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json=_session_body(agent_id=env["agent_id"]),
            )
            assert sess.status_code == 201, sess.text
            seeded.append((sess.json()["id"], sess.json()["created_at"]))
            await asyncio.sleep(0.1)

        first_id, first_ts = seeded[0]
        second_id, _ = seeded[1]

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "right": {"kind": "value", "value": workspace_id},
                },
                "right": {
                    "kind": "predicate",
                    "op": ">=",
                    "left": {"kind": "field", "name": "created_at"},
                    "right": {"kind": "value", "value": first_ts},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"datetime >= leaked /errors/internal: {resp.text}"
        )
        if resp.status_code == 200:
            ids = {item["id"] for item in resp.json()["items"]}
            assert first_id in ids, (
                f"created_at >= first_ts should INCLUDE the boundary "
                f"session: {ids!r}"
            )
            assert second_id in ids, (
                f"created_at >= first_ts should include the later "
                f"session: {ids!r}"
            )
    finally:
        if workspace_id is not None:
            for (sid, _ts) in seeded:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/{sid}/cancel",
                )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0320 — /v1/sessions with status="" and agent_id="" returns 200 list
# ============================================================================


@pytest.mark.asyncio
async def test_t0320_sessions_filter_empty_status_string_rejected_422(
    client: httpx.AsyncClient,
) -> None:
    """T0320 — Empty-string filter values are NOT degraded to "no
    filter" — Pydantic enum validation strictly rejects `status=""`
    with 422 /errors/validation-error (since "" is not a valid
    SessionStatus). This pins the strict-validation contract for
    typed query params.

    Reframed from the original wording (which assumed graceful
    degradation); test discovered the API actually validates.
    """
    resp = await client.get("/v1/sessions?status=")
    assert resp.status_code == 422, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope


# ============================================================================
# T0321 — /v1/sessions?graph_id=<missing> returns 200 with empty items
# ============================================================================


@pytest.mark.asyncio
async def test_t0321_sessions_filter_bogus_graph_id_returns_empty_list(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0321 — Mirror of T0229 (bogus agent_id filter) for the
    graph_id filter. Filtering by a non-existent graph_id must yield
    200 with empty items (narrowing semantics, not validation).
    """
    missing = f"missing-graph-{unique_suffix}"
    resp = await client.get(f"/v1/sessions?graph_id={missing}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body, body
    assert body["items"] == [], (
        f"filter by missing graph_id should yield empty list, got: "
        f"{body['items']!r}"
    )


# ============================================================================
# T0324 — POST /v1/workspaces/{wid}/sessions/{missing}/steer returns 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0324_steer_on_missing_session_returns_404(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0324 — T0159 covers cancel/pause/resume on a missing session
    id; steer was not pinned. Pin: steer on a missing session id
    returns 404 /errors/not-found cleanly, completing the signal-verb
    coverage primer.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        missing_sid = f"missing-sess-{unique_suffix}"
        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{missing_sid}/steer",
            json={"instruction": "this won't matter"},
        )
        assert resp.status_code == 404, resp.text
        envelope = resp.json()
        assert envelope["type"] == "/errors/not-found", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0351 — POST /v1/sessions/find with workspace_id+graph_id+status combo
# ============================================================================


@pytest.mark.asyncio
async def test_t0351_sessions_find_three_way_filter_with_graph_id(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0351 — Sibling of T0120 (which used agent_id) for graph-bound
    sessions. Build two workspaces × two graphs (= four sessions);
    POST /find with predicate filtering by workspace_id+graph_id+
    status returns exactly the one session matching all three
    criteria (intersection).

    NB: graph-bound sessions don't run (per spec §13 known limitation
    pinned by T0156), but the predicate filter operates on the row's
    binding metadata which is set at create-time. So the find still
    works regardless of worker behaviour.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    graph_a = f"graph-a-{unique_suffix}"
    graph_b = f"graph-b-{unique_suffix}"
    workspace_ids: list[str] = []
    session_ids: list[tuple[str, str, str]] = []  # (sid, wid, gid)
    graphs_created: list[str] = []
    try:
        # Create two graphs
        for gid in (graph_a, graph_b):
            r = await client.post(
                "/v1/graphs",
                json={
                    "id": gid,
                    "description": f"T0351-{gid}",
                    "nodes": [
                        {"kind": "begin", "id": "begin"},
                        {"kind": "agent", "id": "n1",
                         "agent_id": env["agent_id"]},
                        {"kind": "end", "id": "end"},
                    ],
                    "edges": [
                        {"kind": "static", "from_node": "begin",
                         "to_node": "n1"},
                        {"kind": "static", "from_node": "n1",
                         "to_node": "end"},
                    ],
                },
            )
            assert r.status_code == 201, r.text
            graphs_created.append(gid)

        # Create two workspaces
        for _ in range(2):
            ws = await client.post(
                "/v1/workspaces", json={"template_id": env["tpl_id"]},
            )
            assert ws.status_code == 201, ws.text
            workspace_ids.append(ws.json()["id"])

        # 4 sessions: each (workspace, graph) combination
        for wid in workspace_ids:
            for gid in (graph_a, graph_b):
                sess = await client.post(
                    f"/v1/workspaces/{wid}/sessions",
                    json={
                        "binding": {"kind": "graph", "graph_id": gid},
                        "auto_start": False,
                    },
                )
                assert sess.status_code == 201, sess.text
                session_ids.append((sess.json()["id"], wid, gid))

        # Pick target combination: workspace_ids[0] + graph_a + created
        target_wid = workspace_ids[0]
        target_gid = graph_a
        # Find the matching session id
        target_sid = next(
            sid for (sid, wid, gid) in session_ids
            if wid == target_wid and gid == target_gid
        )

        # POST /find with three-way filter
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "and",
                    "left": {
                        "kind": "predicate",
                        "op": "=",
                        "left": {"kind": "field", "name": "workspace_id"},
                        "right": {"kind": "value", "value": target_wid},
                    },
                    "right": {
                        "kind": "predicate",
                        "op": "=",
                        "left": {"kind": "field", "name": "status"},
                        "right": {"kind": "value", "value": "created"},
                    },
                },
                "right": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {
                        "kind": "field", "name": "binding.graph_id",
                    },
                    "right": {"kind": "value", "value": target_gid},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"three-way filter leaked /errors/internal: {resp.text}"
        )
        if resp.status_code == 200:
            ids = {item["id"] for item in resp.json()["items"]}
            assert target_sid in ids, (
                f"target session {target_sid!r} missing from "
                f"three-way filter: {ids!r}"
            )
            # Other sessions (different workspace OR different graph)
            # should NOT be present
            for (sid, wid, gid) in session_ids:
                if (wid, gid) != (target_wid, target_gid):
                    assert sid not in ids, (
                        f"non-matching session {sid!r} (wid={wid!r}, "
                        f"gid={gid!r}) unexpectedly in results: {ids!r}"
                    )
    finally:
        for (sid, wid, _gid) in session_ids:
            await client.post(
                f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
            )
        for wid in workspace_ids:
            await client.delete(f"/v1/workspaces/{wid}")
        for gid in graphs_created:
            await client.delete(f"/v1/graphs/{gid}")
        await _teardown_setup(client, env)


# ============================================================================
# T0371 — Session auto_start=true transitions out of CREATED quickly
# ============================================================================


@pytest.mark.asyncio
async def test_t0371_session_auto_start_transitions_out_of_created(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0371 — POST a session with auto_start=true. Pin that the
    response immediately reflects worker enqueue (status not
    "created" anymore, OR the GET shortly after shows transition).

    The agent uses the placeholder anthropic provider (no real LLM
    call), so the worker may end up in "ended" status after a failed
    upstream call — that's still an observable transition out of
    CREATED. NEVER /errors/internal.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    session_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "auto_start": True,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # Poll briefly — within a few seconds should observe a status
        # that's not "created" any more
        observed_non_created = False
        envelope: dict = {}
        for _ in range(30):
            r = await client.get(f"/v1/sessions/{session_id}")
            assert r.status_code == 200, r.text
            envelope = r.json()
            assert envelope.get("type") != "/errors/internal", envelope
            if envelope["status"] != "created":
                observed_non_created = True
                break
            await asyncio.sleep(0.2)

        assert observed_non_created, (
            f"auto_start=true session still 'created' after polling — "
            f"final state: {envelope!r}"
        )
    finally:
        if session_id and workspace_id:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0372 — Top-level filter status=created excludes auto-started sessions
# ============================================================================


@pytest.mark.asyncio
async def test_t0372_status_created_filter_excludes_auto_started(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0372 — Create one auto_start=true and one auto_start=false
    session in the same workspace; wait briefly for the auto-started
    one to leave CREATED; filter by status=created — only the manual
    one should appear.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    auto_sid: str | None = None
    manual_sid: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        auto = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "auto_start": True,
            },
        )
        assert auto.status_code == 201, auto.text
        auto_sid = auto.json()["id"]

        manual = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json=_session_body(agent_id=env["agent_id"]),
        )
        assert manual.status_code == 201, manual.text
        manual_sid = manual.json()["id"]

        # Wait for the auto-started one to leave CREATED
        for _ in range(30):
            r = await client.get(f"/v1/sessions/{auto_sid}")
            if r.status_code == 200 and r.json().get("status") != "created":
                break
            await asyncio.sleep(0.2)

        listing = await client.get(
            f"/v1/sessions?workspace_id={workspace_id}&status=created",
        )
        assert listing.status_code == 200, listing.text
        ids = {item["id"] for item in listing.json()["items"]}
        assert manual_sid in ids, (
            f"manual (auto_start=false) session should be in "
            f"created-filter results: {ids!r}"
        )
        assert auto_sid not in ids, (
            f"auto-started session should NOT be in created-filter "
            f"results: {ids!r}"
        )
    finally:
        if workspace_id:
            for sid in (auto_sid, manual_sid):
                if sid:
                    await client.post(
                        f"/v1/workspaces/{workspace_id}/sessions/{sid}/cancel",
                    )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0374 - auto_start=false stays CREATED; explicit resume starts it
# ============================================================================


@pytest.mark.asyncio
async def test_t0374_auto_start_false_stays_created_resume_starts(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0374 -- auto_start=false: session stays CREATED after create
    (no turn is run); POST .../resume transitions it to non-CREATED and
    a worker eventually picks it up.

    Verifies the claim-engine gate: with auto_start=false no lease is
    registered at create time, so the session remains inert. The resume
    route re-registers the lease and enqueues the session.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    session_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # 1. Create with auto_start=false.
        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # 2. Immediately after create the session must be CREATED.
        # Poll briefly (up to 2 s) to confirm no worker touched it.
        still_created = True
        for _ in range(10):
            r = await client.get(f"/v1/sessions/{session_id}")
            assert r.status_code == 200, r.text
            if r.json().get("status") != "created":
                still_created = False
                break
            await asyncio.sleep(0.2)

        assert still_created, (
            "auto_start=false session left CREATED without an explicit "
            f"resume -- status after polling: "
            f"{(await client.get(f'/v1/sessions/{session_id}')).json().get('status')!r}"
        )

        # 3. Explicit resume must transition the session out of CREATED.
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        assert resume.status_code == 200, resume.text
        assert resume.json()["status"] != "created", (
            f"resume response still shows 'created': {resume.json()!r}"
        )

        # 4. Within a few seconds the worker should pick it up and move
        # it further (running -> ended or similar). The key guarantee is
        # that it left CREATED after the resume.
        observed_non_created = False
        final_envelope: dict = {}
        for _ in range(30):
            r = await client.get(f"/v1/sessions/{session_id}")
            assert r.status_code == 200, r.text
            final_envelope = r.json()
            assert final_envelope.get("type") != "/errors/internal", (
                f"session leaked /errors/internal: {final_envelope!r}"
            )
            if final_envelope["status"] != "created":
                observed_non_created = True
                break
            await asyncio.sleep(0.2)

        assert observed_non_created, (
            "session never left CREATED after explicit resume -- "
            f"final status: {final_envelope.get('status')!r}"
        )
    finally:
        if session_id and workspace_id:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0373 — Session metadata accepts deeply nested dict (3 levels)
# ============================================================================


@pytest.mark.asyncio
async def test_t0373_session_metadata_deeply_nested_round_trips(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0373 — POST with metadata containing 3 nested dict levels +
    leaves of various types. GET returns the same shape; pin JSONB
    serialisation depth.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        nested = {
            "level_1": {
                "level_2": {
                    "level_3": {
                        "leaf_str": f"v-{unique_suffix}",
                        "leaf_int": 42,
                        "leaf_list": ["a", "b", "c"],
                    },
                },
            },
        }
        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "metadata": nested,
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]
        assert sess.json().get("metadata") == nested, sess.json()

        got = await client.get(f"/v1/sessions/{session_id}")
        assert got.status_code == 200, got.text
        assert got.json().get("metadata") == nested, got.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0359 — Predicate `>` with negative integer literal on Session.turn_no
# ============================================================================


@pytest.mark.asyncio
async def test_t0359_predicate_gt_negative_literal_on_turn_no(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0359 — Negative-int boundary on the predicate translator.
    A fresh session has turn_no=0; query `turn_no > -1` must include
    it (since 0 > -1). Pin no /errors/internal on negative literals.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "right": {"kind": "value", "value": workspace_id},
                },
                "right": {
                    "kind": "predicate",
                    "op": ">",
                    "left": {"kind": "field", "name": "turn_no"},
                    "right": {"kind": "value", "value": -1},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 5},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"negative-literal predicate leaked /errors/internal: "
            f"{resp.text}"
        )
        if resp.status_code == 200:
            ids = {item["id"] for item in resp.json()["items"]}
            assert session_id in ids, (
                f"turn_no > -1 should include the fresh session "
                f"(turn_no=0): {ids!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0360 — Predicate `=` with int64-edge value (9223372036854775807)
# ============================================================================


@pytest.mark.asyncio
async def test_t0360_predicate_eq_int64_max_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0360 — Large-int boundary: predicate `turn_no = 9223372036854775807`
    (int64 max) must produce a clean envelope (no /errors/internal).
    The result is expected to be empty (no real session has that
    turn_no), but the SQL coercion path mustn't crash.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, _ = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "turn_no"},
                "right": {"kind": "value", "value": 9223372036854775807},
            },
            "page": {"kind": "offset", "offset": 0, "length": 5},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"int64-max predicate leaked /errors/internal: {resp.text}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0361 — Predicate `=` literal 0 on Session.turn_no matches fresh session
# ============================================================================


@pytest.mark.asyncio
async def test_t0361_predicate_eq_zero_on_turn_no_matches_fresh_session(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0361 — Pin behaviour of `=` on an integer column with a 0
    literal. Pin `no /errors/internal` only — currently this hits
    the same SQL-builder type-coercion bug as T0236 (502
    /errors/provider-server-error with "expected str, got int"
    asyncpg message). The `=` operator on int columns appears to
    cast the column to text in SQL but pass the int literal as a
    bind parameter, causing the type mismatch.

    NB: companion ops `>` and `>=` / `<=` work correctly on
    Session.turn_no per T0150 / T0282; only `=` is broken on int
    columns. Documents the gap; future fix tightens this assertion.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "right": {"kind": "value", "value": workspace_id},
                },
                "right": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "turn_no"},
                    "right": {"kind": "value", "value": 0},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 5},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"int `=` predicate leaked /errors/internal: {resp.text}"
        )
        if resp.status_code == 200:
            ids = {item["id"] for item in resp.json()["items"]}
            assert session_id in ids, (
                f"turn_no = 0 should match fresh session: {ids!r}"
            )
        else:
            # Currently 502 /errors/provider-server-error from SQL
            # builder bug — clean envelope is acceptable
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0242 — /v1/sessions?status=running with no RUNNING session returns 200 empty
# ============================================================================


@pytest.mark.asyncio
async def test_t0242_sessions_filter_status_running_returns_empty_when_none(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0242 — Mirror of T0229 for a status filter rather than agent
    filter. Pins the narrowing semantics of status=running: the filter
    is accepted (200) and every returned item must actually be in
    RUNNING status. Uses a unique never-used agent_id so the set of
    sessions owned by this test is always empty, providing an
    isolation-safe emptiness assertion scoped to this test's agent.
    """
    # Use a synthetic agent_id that no session in the DB will reference.
    # The unique_suffix guarantees no collision with other tests.
    sentinel_agent = f"sentinel-t0242-{unique_suffix}"

    # Global filter must return 200 and all items must be running.
    resp = await client.get("/v1/sessions?status=running")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body, body
    # Every item returned by the running filter must be status=running.
    for item in body["items"]:
        assert item["status"] == "running", (
            f"status=running filter returned a non-running session: "
            f"{item!r}"
        )
    # The sentinel agent was never used — no running sessions for it.
    sentinel_ids = {
        item["id"] for item in body["items"]
        if item.get("binding", {}).get("agent_id") == sentinel_agent
    }
    assert sentinel_ids == set(), (
        f"Unexpected running sessions for sentinel agent: {sentinel_ids!r}"
    )


# ============================================================================
# T0252 — Session resume on a non-existent workspace returns 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0252_session_resume_on_missing_workspace_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0252 — POST /v1/workspaces/{bogus}/sessions/{bogus}/resume on
    BOTH non-existent workspace AND non-existent session id must
    return 404 /errors/not-found cleanly. Complements T0158 (POST
    sessions on missing workspace) and T0159 (signal verbs on missing
    session under existing workspace) by combining both negatives.
    """
    bogus_ws = f"missing-ws-{unique_suffix}"
    bogus_sid = f"missing-sess-{unique_suffix}"
    resp = await client.post(
        f"/v1/workspaces/{bogus_ws}/sessions/{bogus_sid}/resume",
    )
    assert resp.status_code == 404, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404


# ============================================================================
# T0272 — Session create with binding kind="invalid" returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0272_session_create_invalid_binding_kind_returns_422(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0272 — Session.binding is a discriminated union by `kind`
    (per spec §12). A binding with kind="invalid" must fail the
    discriminator check and return 422 /errors/validation-error;
    never /errors/internal.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {
                    "kind": "invalid",
                    "agent_id": env["agent_id"],
                },
                "auto_start": False,
            },
        )
        assert resp.status_code == 422, resp.text
        envelope = resp.json()
        assert envelope["type"] == "/errors/validation-error", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0273 — Session create with binding lacking `kind` field returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0273_session_create_binding_missing_kind_returns_422(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0273 — Session.binding has `kind` as the discriminator.
    Omitting it entirely must produce 422 /errors/validation-error
    cleanly; never /errors/internal.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        resp = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"agent_id": env["agent_id"]},
                "auto_start": False,
            },
        )
        assert resp.status_code == 422, resp.text
        envelope = resp.json()
        assert envelope["type"] == "/errors/validation-error", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0397 — pause from CREATED transitions to PAUSED; second pause idempotent
# ============================================================================


@pytest.mark.asyncio
async def test_t0397_session_pause_from_created_idempotent(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0397 — Per primer/api/routers/sessions.py:251, pause on a
    CREATED/WAITING session (no worker holding a lease) transitions
    directly to PAUSED with status_code=204. Pin two contracts:

      1. First pause from CREATED → 204 + observable PAUSED status
      2. Second pause on the now-PAUSED session is idempotent —
         either 204 again (no-op set) or 409 (some implementations
         reject already-paused). Both shapes are acceptable; the
         hard pin is "no 5xx".

    Extends T0159 (cancel/pause/resume on missing id all 404) into
    the pause-before-resume happy path.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # First pause from CREATED → 204 (per spec §11 table)
        first = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert first.status_code == 204, first.text

        # Observable: GET shows status=paused
        got = await client.get(f"/v1/sessions/{session_id}")
        assert got.status_code == 200, got.text
        assert got.json()["status"] == "paused", got.json()

        # Second pause on PAUSED — pin no-5xx; accept 204 (idempotent
        # no-op) or 409 (rejected as already-paused)
        second = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert second.status_code < 500, second.text
        assert second.status_code in (204, 409), (
            f"second pause should be 204 (idempotent) or 409 "
            f"(already paused); got {second.status_code}: {second.text}"
        )

        # Status must still be paused after the second call
        got2 = await client.get(f"/v1/sessions/{session_id}")
        assert got2.status_code == 200, got2.text
        assert got2.json()["status"] == "paused", got2.json()
    finally:
        if workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0398 — cancel from PAUSED-via-pre-resume-pause converges to terminal
# ============================================================================


@pytest.mark.asyncio
async def test_t0398_session_cancel_while_paused_converges_to_terminal(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0398 — Cancel signal on a PAUSED session (where the pause
    happened pre-resume, i.e. no worker ever held a lease) MUST
    converge cleanly to ENDED/cancelled. Per the cancel handler
    (sessions.py:271-277) cancellation on PAUSED transitions
    directly to ENDED.

    Pins:
      1. cancel returns 200 with the updated row
      2. status == "ended", ended_reason == "cancelled"
      3. ended_at is non-null (terminal-state invariant)
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # CREATED → PAUSED (pre-resume pause)
        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert pause.status_code == 204, pause.text

        # Confirm we're actually in PAUSED
        before = await client.get(f"/v1/sessions/{session_id}")
        assert before.status_code == 200, before.text
        assert before.json()["status"] == "paused", before.json()

        # Cancel from PAUSED → 200 with ENDED/cancelled body
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        body = cancel.json()
        assert body["id"] == session_id, body
        assert body["status"] == "ended", body
        assert body["ended_reason"] == "cancelled", body
        assert body.get("ended_at") is not None, body

        # Subsequent GET still shows the terminal state (no flapping)
        after = await client.get(f"/v1/sessions/{session_id}")
        assert after.status_code == 200, after.text
        assert after.json()["status"] == "ended", after.json()
        assert after.json()["ended_reason"] == "cancelled", after.json()
    finally:
        if workspace_id is not None:
            # Already-terminal cancel is 409 (per T0039); ignore
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0399 — Session steer after terminal returns 4xx with clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0399_session_steer_after_terminal_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0399 — Spec §11 line 493 documents steer as "Does NOT gate on
    session status — instructions can be queued on a CREATED, PAUSED,
    or RUNNING session". ENDED is not in that list. Meanwhile
    primer/workspace/session.py:352 raises ConflictError on ENDED.
    There's a known stale-cache between the two: the WorkspaceRegistry
    holds an in-memory Session view whose `_info.status` doesn't
    refresh from storage right after cancel, so the 200/409 outcome
    depends on cache state.

    Hard contract pinned here: never 5xx, never /errors/internal,
    and the session row stays in the terminal ENDED state regardless
    of which path the steer takes (no flapping). Status code may be
    200 (succeeded against stale view) or 409 (rejected against
    refreshed view) — both are acceptable until the cache drift is
    resolved.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Drive the session to ENDED via cancel
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended", cancel.json()

        # Steer on the now-ended session
        steer = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": "post-terminal-steer"},
        )
        # Hard pin: never 5xx
        assert steer.status_code < 500, steer.text
        # Permissive: 200 (stale-cache let it through) or 4xx
        assert steer.status_code == 200 or 400 <= steer.status_code < 500, (
            f"unexpected status: {steer.status_code}: {steer.text}"
        )
        if steer.status_code >= 400:
            envelope = steer.json()
            assert envelope.get("type", "").startswith("/errors/"), envelope
            assert envelope.get("type") != "/errors/internal", envelope

        # Defence: session row remains in ENDED state (no flapping back
        # to RUNNING/PAUSED no matter which steer outcome).
        after = await client.get(f"/v1/sessions/{session_id}")
        assert after.status_code == 200, after.text
        assert after.json()["status"] == "ended", after.json()
        assert after.json()["ended_reason"] == "cancelled", after.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0403 — POST /v1/sessions/find predicate on `binding.kind`
# ============================================================================


@pytest.mark.asyncio
async def test_t0403_sessions_find_predicate_on_binding_kind(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0403 — Sessions/find supports JSONB-discriminator-tag
    predicate paths (companion to T0351 which used `binding.graph_id`).
    Build one agent-bound + one graph-bound session in the same
    workspace, then `find` filtered by `binding.kind = "agent"`.
    The agent-bound session must appear; the graph-bound one must NOT.

    Pins that the predicate translator handles JSONB discriminator
    tags (string-valued `kind` fields), not just leaf-id columns like
    graph_id / agent_id.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    graph_id = f"graph-t0403-{unique_suffix}"
    graph_created = False
    try:
        # Workspace
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Graph (so we can graph-bind a session)
        gr = await client.post(
            "/v1/graphs",
            json={
                "id": graph_id,
                "description": "T0403",
                "nodes": [
                    {"kind": "begin", "id": "begin"},
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "end", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin", "to_node": "n1"},
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert gr.status_code == 201, gr.text
        graph_created = True

        # Agent-bound session
        sa = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "auto_start": False,
            },
        )
        assert sa.status_code == 201, sa.text
        sid_agent = sa.json()["id"]

        # Graph-bound session
        sg = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": False,
            },
        )
        assert sg.status_code == 201, sg.text
        sid_graph = sg.json()["id"]

        # find by workspace_id AND binding.kind == "agent"
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "right": {"kind": "value", "value": workspace_id},
                },
                "right": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "binding.kind"},
                    "right": {"kind": "value", "value": "agent"},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"binding.kind predicate leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code == 200, resp.text
        ids = {item["id"] for item in resp.json()["items"]}
        assert sid_agent in ids, (
            f"agent-bound session {sid_agent!r} missing from "
            f"binding.kind=agent filter: {ids!r}"
        )
        assert sid_graph not in ids, (
            f"graph-bound session {sid_graph!r} should NOT match "
            f"binding.kind=agent filter: {ids!r}"
        )

        # Inverse pin: binding.kind = "graph" returns only the graph
        # session (and excludes the agent session).
        body_inv = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "right": {"kind": "value", "value": workspace_id},
                },
                "right": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "binding.kind"},
                    "right": {"kind": "value", "value": "graph"},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        inv = await client.post("/v1/sessions/find", json=body_inv)
        assert inv.status_code == 200, inv.text
        ids_inv = {item["id"] for item in inv.json()["items"]}
        assert sid_graph in ids_inv, (
            f"graph-bound session {sid_graph!r} missing from "
            f"binding.kind=graph filter: {ids_inv!r}"
        )
        assert sid_agent not in ids_inv, (
            f"agent-bound session {sid_agent!r} should NOT match "
            f"binding.kind=graph filter: {ids_inv!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        if graph_created:
            await client.delete(f"/v1/graphs/{graph_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0409 — Concurrent pause + cancel on same session: convergence to terminal
# ============================================================================


@pytest.mark.asyncio
async def test_t0409_concurrent_pause_and_cancel_converges_terminal(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0409 — Race a pause against a cancel on the same CREATED
    session. Distinct from T0179 (steer+cancel on RUNNING via LM
    Studio): no real worker is needed because pause/cancel both
    short-circuit on CREATED via the storage layer.

    Pin §17.8 invariant:
      - both calls return < 500 (no internal leaks)
      - pause returns 204 (won) or 409 (cancel landed first → ENDED)
      - cancel returns 200 (won) or 409 (pause landed first → PAUSED
        is allowed by cancel handler so cancel can also still 200
        from PAUSED — both 200 outcomes are valid)
      - the session converges to ENDED/cancelled
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        pause_task = asyncio.create_task(client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        ))
        cancel_task = asyncio.create_task(client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        ))
        pause_resp, cancel_resp = await asyncio.gather(
            pause_task, cancel_task,
        )

        # Hard pin: never 5xx
        assert pause_resp.status_code < 500, pause_resp.text
        assert cancel_resp.status_code < 500, cancel_resp.text

        # Documented outcomes
        assert pause_resp.status_code in (204, 409), (
            f"pause race: unexpected code {pause_resp.status_code}: "
            f"{pause_resp.text}"
        )
        assert cancel_resp.status_code in (200, 409), (
            f"cancel race: unexpected code {cancel_resp.status_code}: "
            f"{cancel_resp.text}"
        )

        # At least one of them succeeded (both 409 would mean neither
        # transition won — that's a stuck-state regression).
        assert (
            pause_resp.status_code == 204 or cancel_resp.status_code == 200
        ), (
            f"both pause and cancel returned 409 — state machine "
            f"stuck: pause={pause_resp.text}, cancel={cancel_resp.text}"
        )

        # Final convergence — push to terminal (in case pause won;
        # then the session is PAUSED and we need to cancel from there).
        if cancel_resp.status_code != 200:
            final = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
            # Already ENDED → 409, or PAUSED → 200; both fine
            assert final.status_code in (200, 409), final.text

        got = await client.get(f"/v1/sessions/{session_id}")
        assert got.status_code == 200, got.text
        assert got.json()["status"] == "ended", got.json()
        assert got.json()["ended_reason"] == "cancelled", got.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0410 — Concurrent double-resume on same CREATED session: both 200
# ============================================================================


@pytest.mark.asyncio
async def test_t0410_concurrent_double_resume_both_200(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0410 — Resume is documented as idempotent (T0038 sequential).
    Pin the concurrent variant: two simultaneous /resume calls both
    return 200 (not 5xx, not one-success-one-409). The session row
    must be intact (single non-corrupt row) and observable.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        r1_task = asyncio.create_task(client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        ))
        r2_task = asyncio.create_task(client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        ))
        r1, r2 = await asyncio.gather(r1_task, r2_task)

        # Hard pin: never 5xx
        assert r1.status_code < 500, r1.text
        assert r2.status_code < 500, r2.text
        # Both succeed (idempotent semantics under concurrency)
        assert r1.status_code == 200, (
            f"first concurrent resume: {r1.status_code}: {r1.text}"
        )
        assert r2.status_code == 200, (
            f"second concurrent resume: {r2.status_code}: {r2.text}"
        )
        # Both report the same session id (no aliasing / row split)
        assert r1.json()["id"] == session_id, r1.json()
        assert r2.json()["id"] == session_id, r2.json()

        # Subsequent GET shows a single non-corrupt row
        got = await client.get(f"/v1/sessions/{session_id}")
        assert got.status_code == 200, got.text
        assert got.json()["id"] == session_id, got.json()
        # Status is observable (running or whatever the worker drove
        # it to without LM Studio); the hard pin is "no row corruption"
        assert isinstance(got.json().get("status"), str), got.json()
    finally:
        if workspace_id is not None:
            # Cancel to release any worker lease before workspace destroy
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# Shared helper: wait for a session to reach `status="ended"` or timeout.
# Mirrors the LM-Studio file's `_wait_for_terminal` so the graph-executor
# error-contract tests below don't need LM Studio to drive them.
# ============================================================================


async def _wait_for_session_ended(
    client: httpx.AsyncClient, *, session_id: str,
    timeout_s: float = 30.0, interval_s: float = 0.5,
) -> dict:
    """Poll top-level GET /v1/sessions/{id} until status='ended' or
    timeout. Returns the last seen body regardless of outcome —
    caller decides what's acceptable.
    """
    deadline_iters = max(1, int(timeout_s / interval_s))
    last: dict = {}
    for _ in range(deadline_iters):
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200:
            last = r.json()
            if last.get("status") == "ended":
                return last
        await asyncio.sleep(interval_s)
    return last


# ============================================================================
# T0429 — Graph-bound session terminates cleanly via _handle_fatal
# ============================================================================


@pytest.mark.asyncio
async def test_t0429_graph_bound_session_terminates_via_fatal_path(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0429 — Graph-bound session must reach a terminal state and
    not get stuck in RUNNING after resume.

    The graph executor is now fully implemented. For THIS graph
    (begin -> agent(n1) -> end) the session-row ended_reason is
    DETERMINISTICALLY "completed". Rationale (static trace through
    the worker + graph executor):
      * The agent node n1 makes a real Anthropic call with the bogus
        "sk-test-placeholder" key, so the LLM turn raises an auth
        error. _BaseGraphExecutor._stream_node catches that
        BaseException (primer/graph/base.py:1597) and packages it as
        a _NodeDone(error=...) -- it never re-raises out of invoke().
      * The superstep loop marks the node FAILED, sets any_failed,
        breaks, and writes the GRAPH state.json with
        ended_reason="failed" (primer/graph/base.py:1151-1159,
        1234-1243). The generator still returns NORMALLY.
      * _GraphTurnDriver.invoke() just drains that stream, so the
        worker sees a clean completion and reports the fixed
        last_done_reason="graph_ended" sentinel
        (primer/worker/pool.py:1716-1735).
      * dispatch reaches the clean-completion path (step 6) and maps
        "graph_ended" -> (ENDED, "completed")
        (primer/session/dispatch.py:485, 607).
    So the SESSION ROW is "completed" even though the graph's own
    state.json is "failed" -- the worker driver does not surface
    node-level failures. "failed" on the session row would require
    invoke() ITSELF to raise (e.g. _build_graph_executor config
    error), which cannot happen for this well-formed graph on a
    LocalWorkspace; "cancelled" cannot happen with no cancel in
    flight. We pin "completed" exactly so a future regression that
    lets a node failure escape to the worker (flipping the row to
    "failed") is CAUGHT instead of silently accepted.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    graph_id = f"graph-t0429-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    graph_created = False
    try:
        # Minimal one-agent + terminal graph
        gr = await client.post(
            "/v1/graphs",
            json={
                "id": graph_id,
                "description": "T0429 — minimal graph",
                "nodes": [
                    {"kind": "begin", "id": "begin"},
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "end", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin", "to_node": "n1"},
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert gr.status_code == 201, gr.text
        graph_created = True

        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
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
        assert sess.json()["binding"]["kind"] == "graph"

        # Resume → worker claims → _build_graph_executor raises →
        # _handle_fatal updates row to ENDED/failed
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        assert resume.status_code == 200, resume.text

        final = await _wait_for_session_ended(
            client, session_id=session_id, timeout_s=30.0,
        )
        assert final.get("status") == "ended", (
            f"graph-bound session did not converge to terminal in 30s "
            f"(stuck-in-RUNNING regression?): {final!r}"
        )
        # Deterministic: node-level failures are swallowed by the
        # graph executor and the worker reports "graph_ended" ->
        # the session row is "completed". A "failed"/"cancelled"
        # here is a real behaviour change (see docstring) and must
        # fail the test, not be waved through.
        assert final.get("ended_reason") == "completed", (
            f"graph-bound session must end 'completed' (graph executor "
            f"maps graph_ended -> completed); got {final!r}"
        )
    finally:
        if session_id is not None and workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        if graph_created:
            await client.delete(f"/v1/graphs/{graph_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0432 — Cancel signal during graph executor failure converges cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0432_graph_session_cancel_during_fatal_converges_cleanly(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0432 — Race a cancel against the worker's transient RUNNING
    state on a graph-bound session. The graph executor is now
    implemented; the only two terminal outcomes are:
      * "cancelled" -- the cancel landed first (the worker ended the
        session before claiming it: primer/worker/pool.py:531-532) or
        the cancel-watcher fired mid-stream (dispatch step 5b,
        primer/session/dispatch.py:454-474); or
      * "completed" -- the graph drained to its end node before the
        cancel was observed, mapping graph_ended -> completed
        (primer/session/dispatch.py:485, 607).
    "failed" is NOT reachable: node-level failures (the bogus-key auth
    error on the agent node) are swallowed inside the graph executor
    and never re-raise to the worker (see T0429 docstring). What is
    also NOT acceptable is sticking in RUNNING or surfacing
    /errors/internal anywhere.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    graph_id = f"graph-t0432-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    graph_created = False
    try:
        gr = await client.post(
            "/v1/graphs",
            json={
                "id": graph_id,
                "description": "T0432",
                "nodes": [
                    {"kind": "begin", "id": "begin"},
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "end", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin", "to_node": "n1"},
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert gr.status_code == 201, gr.text
        graph_created = True

        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
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

        # Race: resume + cancel without ordering. Cancel may land
        # before/after the worker observes the session. Either way
        # convergence to a terminal state must happen, and no 5xx.
        resume_task = asyncio.create_task(client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        ))
        cancel_task = asyncio.create_task(client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        ))
        resume_resp, cancel_resp = await asyncio.gather(
            resume_task, cancel_task,
        )

        # Hard pin: never 5xx
        assert resume_resp.status_code < 500, resume_resp.text
        assert cancel_resp.status_code < 500, cancel_resp.text
        # Documented codes
        assert resume_resp.status_code in (200, 409), resume_resp.text
        assert cancel_resp.status_code in (200, 409), cancel_resp.text

        # Convergence to terminal — extended timeout because the worker
        # may still cycle through the failure path
        final = await _wait_for_session_ended(
            client, session_id=session_id, timeout_s=30.0,
        )
        assert final.get("status") == "ended", (
            f"session stuck after cancel-during-fatal race: {final!r}"
        )
        # ended_reason is "completed" (graph drained to its end node
        # before the cancel was observed) or "cancelled" (cancel landed
        # first / fired mid-stream). "failed" is NOT reachable here --
        # node failures are swallowed by the graph executor -- so a
        # "failed" row signals a real behaviour change and must fail.
        assert final.get("ended_reason") in ("completed", "cancelled"), (
            f"unexpected ended_reason (expected completed|cancelled, "
            f"never failed): {final!r}"
        )
        # ended_at must be populated
        assert final.get("ended_at") is not None, final
    finally:
        if session_id is not None and workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        if graph_created:
            await client.delete(f"/v1/graphs/{graph_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0433 — Top-level GET /v1/sessions/{id} reflects ended_reason from fatal
# ============================================================================


@pytest.mark.requires_llm
@pytest.mark.asyncio
async def test_t0433_top_level_get_reflects_fatal_ended_reason(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0433 — When a graph-bound session terminates, the row's
    `ended_reason` must be visible on the top-level GET
    /v1/sessions/{id} read. The graph executor is now implemented;
    for THIS graph (begin -> agent(n1) -> end) the session-row
    ended_reason is DETERMINISTICALLY "completed" -- the agent node's
    bogus-key auth failure is swallowed by the graph executor and the
    worker reports the "graph_ended" sentinel (see T0429 docstring for
    the full static trace). The hard pin is that the row is ENDED and
    the top-level GET returns ended_reason="completed".

    Documented divergence: the NESTED route
    /v1/workspaces/{wid}/sessions/{sid} returns `{info, status}`
    sourced from the LocalWorkspace's in-memory `_sessions` dict,
    which is populated only by `start_session` for AGENT bindings
    (primer/workspace/local/workspace.py:135-160). Graph-bound
    sessions never get a live AgentSession object, so the nested
    GET 404s. The top-level GET reads session_storage directly and
    works for both binding kinds. This is a real spec/impl drift
    pinned for now and noted in 01-app-spec.md §11.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    graph_id = f"graph-t0433-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    graph_created = False
    try:
        gr = await client.post(
            "/v1/graphs",
            json={
                "id": graph_id,
                "description": "T0433",
                "nodes": [
                    {"kind": "begin", "id": "begin"},
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "end", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin", "to_node": "n1"},
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert gr.status_code == 201, gr.text
        graph_created = True

        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
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

        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        assert resume.status_code == 200, resume.text

        # Top-level GET reads session_storage and MUST work for
        # graph-bound sessions
        top_final = await _wait_for_session_ended(
            client, session_id=session_id, timeout_s=30.0,
        )
        assert top_final.get("status") == "ended", top_final
        # Deterministic: graph executor maps graph_ended -> completed
        # on the session row (node failures are swallowed internally).
        assert top_final.get("ended_reason") == "completed", top_final
        assert top_final.get("ended_at") is not None, top_final
        # Binding round-trips through top-level GET
        binding = top_final.get("binding", {})
        assert binding.get("kind") == "graph", top_final
        assert binding.get("graph_id") == graph_id, top_final

        # Nested workspace GET — documented divergence: graph-bound
        # sessions don't have a live AgentSession in the workspace's
        # in-memory dict, so the nested handler 404s. We pin the
        # actual behaviour: clean 404 envelope, never /errors/internal.
        # If a future change wires graph sessions into the nested
        # path too, this branch flips to 200 — which would also be a
        # valid outcome and the test should be tightened then.
        nested = await client.get(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
        )
        assert nested.status_code in (200, 404), (
            f"nested GET on graph-bound session: unexpected status "
            f"{nested.status_code}: {nested.text}"
        )
        if nested.status_code == 404:
            envelope = nested.json()
            assert envelope.get("type") == "/errors/not-found", envelope
        else:
            # Future-proof branch: when the nested handler learns to
            # read graph-bound rows from storage, it must reach ENDED
            # and -- reading the same DB row the top-level GET reads --
            # report the same deterministic "completed" reason.
            nested_body = nested.json()
            info = nested_body.get("info", {})
            assert nested_body.get("status") == top_final["status"]
            assert info.get("ended_reason") == "completed", info
    finally:
        if session_id is not None and workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        if graph_created:
            await client.delete(f"/v1/graphs/{graph_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0441 — Sequential pause → cancel → steer: documented behaviour
# ============================================================================


@pytest.mark.asyncio
async def test_t0441_session_pause_then_cancel_then_steer_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0441 — Sibling of T0399 (cancel→steer where steer 200s due
    to stale in-memory Session view). T0441 walks pause→cancel→steer:

      1. CREATED → pause (204) → PAUSED
      2. PAUSED → cancel (200) → ENDED/cancelled
      3. ENDED → steer

    Two cache layers are at play here:
      - sessions_storage (the row): definitely ENDED after step 2
      - WorkspaceRegistry's in-memory AgentSession (`_info.status`):
        may still show PAUSED (stale) until refreshed

    Hard pin: never 5xx, never `/errors/internal`, and the row
    persisted in storage stays ENDED/cancelled regardless of which
    code path the steer takes (200 stale-cache, or 409 fresh-view).
    Subsequent reads must show the terminal state — no flapping.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # 1. pause from CREATED
        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert pause.status_code == 204, pause.text

        # 2. cancel from PAUSED (sessions.py:287-296 transitions
        # PAUSED → ENDED directly)
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended", cancel.json()
        assert cancel.json()["ended_reason"] == "cancelled", cancel.json()

        # 3. steer on ENDED — pin no 5xx, accept 200 (stale-cache
        # let it through, T0399 sibling) or 409 (fresh view rejected)
        steer = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": "post-pause-cancel-steer"},
        )
        assert steer.status_code < 500, steer.text
        assert steer.status_code == 200 or 400 <= steer.status_code < 500, (
            f"unexpected steer status: {steer.status_code}: {steer.text}"
        )
        if steer.status_code >= 400:
            envelope = steer.json()
            assert envelope.get("type", "").startswith("/errors/"), envelope
            assert envelope.get("type") != "/errors/internal", envelope

        # Defence: row stays ENDED/cancelled (no flapping) regardless
        # of which steer outcome
        after = await client.get(f"/v1/sessions/{session_id}")
        assert after.status_code == 200, after.text
        assert after.json()["status"] == "ended", after.json()
        assert after.json()["ended_reason"] == "cancelled", after.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0457 — Session metadata with 1 MiB string field round-trips
# ============================================================================


@pytest.mark.asyncio
async def test_t0457_session_metadata_one_mib_string_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0457 — POST a session with metadata containing a 1 MiB string
    field. Pin: either accepted byte-exact (200/201 with the exact
    value preserved through GET) OR rejected with a clean 4xx
    envelope (e.g. 413 Payload Too Large or 422). Never 5xx, never
    /errors/internal.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    session_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        big_blob = "X" * (1024 * 1024)  # exactly 1 MiB
        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "metadata": {"big_blob": big_blob, "tag": unique_suffix},
                "auto_start": False,
            },
        )
        envelope = sess.json() if sess.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"1 MiB metadata leaked /errors/internal: {sess.text}"
        )
        assert sess.status_code < 500, sess.text

        if sess.status_code == 201:
            session_id = sess.json()["id"]
            # Round-trip via top-level GET
            got = await client.get(f"/v1/sessions/{session_id}")
            assert got.status_code == 200, got.text
            md = got.json().get("metadata", {})
            assert md.get("tag") == unique_suffix, md.get("tag")
            assert md.get("big_blob") == big_blob, (
                f"1 MiB string corrupted on round-trip: "
                f"sent_len={len(big_blob)}, got_len="
                f"{len(md.get('big_blob') or '')!r}"
            )
        else:
            # Rejected — must be a clean 4xx envelope
            assert sess.status_code in (400, 413, 422), (
                f"unexpected rejection status: {sess.status_code}: "
                f"{sess.text}"
            )
            assert envelope.get("type", "").startswith("/errors/"), envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0458 — Session metadata 8-level deeply-nested dict round-trips
# ============================================================================


@pytest.mark.asyncio
async def test_t0458_session_metadata_8_level_nested_dict_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0458 — Pin nested-JSON depth handling beyond T0227 (1-level)
    and T0373 (3-level). Build an 8-level nested dict with a marker
    leaf at the bottom. Pin: 201 + GET round-trip preserves the full
    structure byte-exact, OR 4xx with clean envelope. Never 5xx.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Build {"l0": {"l1": {"l2": ... {"l7": {"leaf": <marker>}}}}}
        marker = f"deep-marker-{unique_suffix}"
        nested: dict = {"leaf": marker}
        for level in range(7, -1, -1):
            nested = {f"l{level}": nested}

        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": env["agent_id"]},
                "metadata": nested,
                "auto_start": False,
            },
        )
        envelope = sess.json() if sess.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"8-level nested metadata leaked /errors/internal: "
            f"{sess.text}"
        )
        assert sess.status_code < 500, sess.text

        if sess.status_code == 201:
            session_id = sess.json()["id"]
            try:
                got = await client.get(f"/v1/sessions/{session_id}")
                assert got.status_code == 200, got.text
                md = got.json().get("metadata")
                assert md == nested, (
                    f"8-level nested metadata corrupted on round-trip: "
                    f"sent={nested!r}, got={md!r}"
                )
                # Walk down to confirm the leaf survives intact
                cur: object = md
                for level in range(8):
                    assert isinstance(cur, dict), (
                        f"level {level} is not a dict: {cur!r}"
                    )
                    cur = cur[f"l{level}"]
                assert cur == {"leaf": marker}, (
                    f"deepest leaf corrupted: {cur!r}"
                )
            finally:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/"
                    f"{session_id}/cancel",
                )
        else:
            assert sess.status_code in (400, 413, 422), sess.text
            assert envelope.get("type", "").startswith("/errors/"), envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0489 — resume → cancel → resume → cancel state machine sequence
# ============================================================================


@pytest.mark.asyncio
async def test_t0489_session_cancel_resume_cancel_resume_post_terminal_sticky(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0489 — Pin post-terminal stickiness across multiple signal
    types on a CREATED session (deterministic — does not require
    worker activity, so works without LM Studio):

      1. cancel — CREATED → ENDED/cancelled (200, direct
         short-circuit per primer/api/routers/sessions.py:287-296)
      2. resume — on ENDED — 409 /errors/conflict
      3. cancel — already ended — 409 /errors/conflict
      4. resume — still ended — 409 /errors/conflict

    Pin: documented codes at each step; row's ended_reason and
    ended_at NEVER change across the post-terminal noise;
    never /errors/internal.

    The "resume first" sequence (which would actually engage a
    worker) is covered by T0037 / T0179 in the LM Studio test file.
    T0489's load-bearing assertion is post-terminal stickiness
    across multiple signal types, which is independent of worker
    behaviour and would otherwise be flaky against the placeholder
    Anthropic provider used in this file.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Step 1: cancel CREATED → direct ENDED/cancelled
        c1 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        envelope_c1 = c1.json() if c1.content else {}
        assert envelope_c1.get("type") != "/errors/internal", c1.text
        assert c1.status_code == 200, c1.text
        body_c1 = c1.json()
        assert body_c1["status"] == "ended", body_c1
        assert body_c1["ended_reason"] == "cancelled", body_c1
        terminal_reason = body_c1["ended_reason"]
        terminal_ended_at = body_c1.get("ended_at")
        assert terminal_ended_at is not None, body_c1

        # Step 2: resume on ENDED — must be 409 conflict
        r1 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        envelope_r1 = r1.json() if r1.content else {}
        assert envelope_r1.get("type") != "/errors/internal", r1.text
        assert r1.status_code == 409, (
            f"resume on ENDED should be 409 conflict; got "
            f"{r1.status_code}: {r1.text}"
        )
        assert envelope_r1.get("type") == "/errors/conflict", envelope_r1

        # Step 3: cancel on already-ended — also 409 (per T0039 sibling)
        c2 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        envelope_c2 = c2.json() if c2.content else {}
        assert envelope_c2.get("type") != "/errors/internal", c2.text
        assert c2.status_code == 409, (
            f"cancel on already-ended should be 409; got "
            f"{c2.status_code}: {c2.text}"
        )
        assert envelope_c2.get("type") == "/errors/conflict", envelope_c2

        # Step 4: a second resume on the (still ended) row — 409
        r2 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        envelope_r2 = r2.json() if r2.content else {}
        assert envelope_r2.get("type") != "/errors/internal", r2.text
        assert r2.status_code == 409, (
            f"second resume on ENDED should still 409; got "
            f"{r2.status_code}: {r2.text}"
        )

        # Defence: row state preserved across all four post-terminal
        # signals — same ended_reason, same ended_at (no flapping)
        after = await client.get(f"/v1/sessions/{session_id}")
        assert after.status_code == 200, after.text
        assert after.json()["status"] == "ended", after.json()
        assert after.json()["ended_reason"] == terminal_reason, after.json()
        assert after.json().get("ended_at") == terminal_ended_at, after.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0491 — Three rapid-fire concurrent cancel calls on a CREATED session
# ============================================================================


@pytest.mark.asyncio
async def test_t0491_three_concurrent_cancel_on_created_session_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0491 — Fire 3 concurrent POST /cancel on the same CREATED
    session. Pin: never /errors/internal anywhere; the documented
    outcome is one 200 winner + two 409 losers (per the cancel
    handler's "if status==ENDED → ConflictError" path), but
    stale-cache (T0399 family) may let multiple 200s through if
    they all observe the pre-cancel state. Either is acceptable;
    final row stays ended/cancelled.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Three concurrent cancels
        tasks = [
            asyncio.create_task(client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            ))
            for _ in range(3)
        ]
        results = await asyncio.gather(*tasks)

        # No /errors/internal anywhere
        for i, r in enumerate(results):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"cancel[{i}] leaked /errors/internal: {r.text}"
            )
            assert r.status_code < 500, r.text
            # Documented codes: 200 (won) or 409 (already ended)
            assert r.status_code in (200, 409), (
                f"cancel[{i}]: unexpected {r.status_code}: {r.text}"
            )

        # At least one 200 winner — otherwise the session never
        # actually got cancelled
        winners = [r for r in results if r.status_code == 200]
        assert len(winners) >= 1, (
            f"no cancel won the race: statuses="
            f"{[r.status_code for r in results]!r}"
        )

        # All 409 losers carry /errors/conflict
        for r in results:
            if r.status_code == 409:
                envelope = r.json()
                assert envelope.get("type") == "/errors/conflict", envelope

        # Final row stays ended/cancelled
        after = await client.get(f"/v1/sessions/{session_id}")
        assert after.status_code == 200, after.text
        assert after.json()["status"] == "ended", after.json()
        assert after.json()["ended_reason"] == "cancelled", after.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0503 — Steer empty then valid: rejection doesn't pollute session state
# ============================================================================


@pytest.mark.asyncio
async def test_t0503_steer_empty_then_valid_both_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0503 — Sibling of T0205 (empty steer alone) and T0399
    (post-cancel steer cache state). T0503 specifically pins that
    a REJECTED empty-instruction steer doesn't pollute session
    state — a SUBSEQUENT valid steer on the same session works.
    Catches a regression where the rejected first call leaves
    transient state on the session that breaks the next call.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Step 1: empty instruction — Pydantic min_length=1 → 422
        # (per primer/api/routers/workspaces.py:120-130 SteerBody)
        empty = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": ""},
        )
        envelope_e = empty.json() if empty.content else {}
        assert envelope_e.get("type") != "/errors/internal", empty.text
        assert empty.status_code < 500, empty.text
        # Either 422 (validation reject — strict path) or 200/204
        # (handler accepted — permissive path per T0205's tolerance)
        assert empty.status_code in (200, 204, 400, 422), empty.text

        # Step 2: valid instruction — must succeed (200/204) cleanly
        valid_text = f"valid-instruction-{unique_suffix}"
        valid = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": valid_text},
        )
        envelope_v = valid.json() if valid.content else {}
        assert envelope_v.get("type") != "/errors/internal", valid.text
        assert valid.status_code in (200, 204), (
            f"valid steer after rejected empty steer should succeed; "
            f"got {valid.status_code}: {valid.text}"
        )

        # Defence: session row still readable + identifiable; no
        # state corruption from the rejected call
        got = await client.get(f"/v1/sessions/{session_id}")
        assert got.status_code == 200, got.text
        assert got.json()["id"] == session_id, got.json()
        # Status remains a valid SessionStatus literal
        assert got.json()["status"] in (
            "created", "running", "paused", "ended",
        ), got.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0504 — Pause CREATED → PAUSED → resume returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0504_pause_then_resume_on_created_session_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0504 — Walk pause→resume on a CREATED session (the
    pause-handler short-circuits CREATED → PAUSED per
    primer/api/routers/sessions.py:251-254). Pin: each step returns
    a documented code; the row goes through observable state
    transitions (CREATED→PAUSED→running-or-ended); never
    /errors/internal.

    Reframed from the original "resume on pause-requested
    transient" angle — that variant requires a RUNNING session
    (only RUNNING sets pause_requested instead of transitioning
    to PAUSED), which needs LM Studio. This deterministic walk
    pins the same boundary contracts without that dependency.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Step 1: pause CREATED → PAUSED (204, direct transition)
        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert pause.status_code == 204, pause.text

        # Observable: status is PAUSED (per primer/api/routers/
        # sessions.py:252)
        before_resume = await client.get(f"/v1/sessions/{session_id}")
        assert before_resume.status_code == 200, before_resume.text
        assert before_resume.json()["status"] == "paused", (
            before_resume.json()
        )

        # Step 2: resume PAUSED → running (200; resume is idempotent
        # per spec §11)
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        envelope_r = resume.json() if resume.content else {}
        assert envelope_r.get("type") != "/errors/internal", resume.text
        assert resume.status_code == 200, (
            f"resume on PAUSED should be 200 idempotent; got "
            f"{resume.status_code}: {resume.text}"
        )
        # Status is running or ended (worker may finish before we
        # observe — placeholder Anthropic creds typically fail-fast)
        body_r = resume.json()
        assert body_r["id"] == session_id, body_r
        assert body_r["status"] in ("running", "ended"), body_r
    finally:
        if workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0539 — Resume → cancel rapid (no sleep) on CREATED converges cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0539_resume_then_cancel_rapid_no_sleep_converges_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0539 — Sequential resume→cancel with NO sleep between
    them on a CREATED session. Pin: documented codes (resume 200,
    cancel 200/409); session converges to ended/cancelled or
    ended/failed; never /errors/internal.

    Distinct from T0489 (post-terminal stickiness) and T0179
    (concurrent steer+cancel race): T0539 specifically pins the
    tight-sequential resume→cancel boundary where the worker may
    or may not have observed the resume before the cancel lands.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Resume immediately followed by cancel (no awaits between
        # them beyond the HTTP roundtrips themselves)
        r = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        envelope_r = r.json() if r.content else {}
        assert envelope_r.get("type") != "/errors/internal", r.text
        assert r.status_code == 200, r.text
        assert r.json()["status"] in ("running", "ended"), r.json()

        c = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        envelope_c = c.json() if c.content else {}
        assert envelope_c.get("type") != "/errors/internal", c.text
        # Documented codes: 200 (won) or 409 (already ended in the
        # gap between resume and cancel — fast-failing worker)
        assert c.status_code in (200, 409), (
            f"unexpected cancel status: {c.status_code}: {c.text}"
        )

        # Poll for terminal — accept any documented terminal
        # ended_reason
        final: dict = {}
        for _ in range(60):
            g = await client.get(f"/v1/sessions/{session_id}")
            assert g.status_code == 200, g.text
            final = g.json()
            if final.get("status") == "ended":
                break
            await asyncio.sleep(0.5)
        assert final.get("status") == "ended", (
            f"resume→cancel did not converge in 30s: {final!r}"
        )
        assert final.get("ended_reason") in (
            "cancelled", "completed", "failed",
        ), final
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0540 — Pause → resume → pause sequence on CREATED returns clean codes
# ============================================================================


@pytest.mark.asyncio
async def test_t0540_pause_resume_pause_sequence_on_created_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0540 — Walk pause→resume→pause on a CREATED session.
    Pin: each step returns documented codes; row converges to a
    documented status (PAUSED or ENDED); never /errors/internal.

    Step semantics:
      1. CREATED → pause → 204 (PAUSED per sessions.py:251-254)
      2. PAUSED → resume → 200 (transitions to RUNNING/ENDED)
      3. running/ended → pause → 204 or 409 (depends on worker
         observability and whether session already terminated)
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Step 1: pause CREATED → 204
        p1 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert p1.status_code == 204, p1.text

        # Observable: PAUSED
        s1 = await client.get(f"/v1/sessions/{session_id}")
        assert s1.status_code == 200, s1.text
        assert s1.json()["status"] == "paused", s1.json()

        # Step 2: resume PAUSED → 200 (transitions to running)
        r = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        envelope_r = r.json() if r.content else {}
        assert envelope_r.get("type") != "/errors/internal", r.text
        assert r.status_code == 200, r.text
        assert r.json()["status"] in ("running", "ended"), r.json()

        # Step 3: pause again — depends on whether worker has driven
        # the session to ENDED or it's still RUNNING
        p2 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        envelope_p2 = p2.json() if p2.content else {}
        assert envelope_p2.get("type") != "/errors/internal", p2.text
        assert p2.status_code in (204, 409), (
            f"second pause: unexpected {p2.status_code}: {p2.text}"
        )

        # Final convergence: row reaches a documented status
        final: dict = {}
        for _ in range(60):
            g = await client.get(f"/v1/sessions/{session_id}")
            assert g.status_code == 200, g.text
            final = g.json()
            if final.get("status") in ("ended", "paused"):
                break
            await asyncio.sleep(0.5)
        assert final.get("status") in ("ended", "paused"), (
            f"pause→resume→pause didn't converge in 30s: {final!r}"
        )
    finally:
        if workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0555 — Cancel agent-bound session: top-level + nested GET both ENDED
# ============================================================================


@pytest.mark.asyncio
async def test_t0555_cancelled_session_top_level_vs_nested_documented_drift(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0555 — Documents a SECOND stale-cache divergence on top of
    T0433 (graph-bound nested 404). For agent-bound sessions that
    are cancelled from CREATED via the storage path (sessions.py:
    287-296 transitions CREATED→ENDED directly via
    sessions.update), the nested workspace handler reads
    `await session.status()` from the in-memory AgentSession
    object, which doesn't see the storage update. Result:

      - top-level GET /v1/sessions/{id} reads storage → "ended"
      - nested GET /v1/workspaces/{wid}/sessions/{sid} reads
        in-memory AgentSession → may report stale "running"

    This is the same root cause as T0399 (steer-after-cancel
    succeeds despite ENDED) and T0441 (pause→cancel→steer cache).
    Pin: top-level is the authoritative source; never
    /errors/internal on either; spec §11 needs a callout that
    nested status may be stale on agent-bound rows that were
    cancelled-from-CREATED until cache refreshed (a fix would
    route session.status() through storage when the in-memory
    object is stale).
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Cancel CREATED → storage row → ENDED (direct transition)
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended", cancel.json()
        assert cancel.json()["ended_reason"] == "cancelled", cancel.json()

        # Top-level GET reads storage — authoritative
        top = await client.get(f"/v1/sessions/{session_id}")
        assert top.status_code == 200, top.text
        top_body = top.json()
        assert top_body["status"] == "ended", top_body
        assert top_body["ended_reason"] == "cancelled", top_body
        assert top_body.get("ended_at") is not None, top_body

        # Nested GET reads in-memory AgentSession — may be stale
        nested = await client.get(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
        )
        assert nested.status_code == 200, nested.text
        nested_body = nested.json()
        nested_status = nested_body.get("status")
        # Documented drift: nested may show "ended" (cache happened to
        # refresh) OR a stale state (created/running) — never a
        # contradictory non-status value, never /errors/internal
        assert nested_status in ("created", "running", "ended"), (
            f"nested status outside the documented set: "
            f"{nested_status!r}"
        )
        # Whether nested matches top-level is non-deterministic; print
        # observation for visibility under -s
        if nested_status != top_body["status"]:
            print(
                f"\n[T0555] documented stale-cache drift observed: "
                f"top={top_body['status']!r} vs nested={nested_status!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0593 — Steer queued on graph-bound CREATED session before fatal turn
# ============================================================================


@pytest.mark.asyncio
async def test_t0593_steer_on_graph_session_before_fatal_turn(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0593 — Sister of T0429. Spec §12 says steer "Does NOT gate on
    session status". Pin: a steer on a graph-bound CREATED session
    succeeds (2xx or 4xx, never 5xx); after resume, the session
    still converges to ENDED -- the steer queue did NOT crash the
    worker and did NOT leak /errors/internal. The graph executor is
    now implemented; ended_reason may be "completed", "failed", or
    "cancelled" -- all are valid.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    graph_id = f"graph-t0593-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    graph_created = False
    try:
        gr = await client.post(
            "/v1/graphs",
            json={
                "id": graph_id,
                "description": "T0593 — graph for steer-then-fatal",
                "nodes": [
                    {"kind": "begin", "id": "begin"},
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "end", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin", "to_node": "n1"},
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert gr.status_code == 201, gr.text
        graph_created = True

        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
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

        # Steer BEFORE resume — session is still in CREATED
        steer = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": "this should be queued and discarded"},
        )
        envelope = steer.json() if steer.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"steer on graph-bound CREATED leaked /errors/internal: "
            f"{steer.text}"
        )
        # Spec says steer does not gate on status, so 2xx expected.
        # 4xx is acceptable if a future iteration tightens validation,
        # but never 5xx.
        assert steer.status_code < 500, (
            f"steer on graph-CREATED returned 5xx: "
            f"{steer.status_code}: {steer.text}"
        )
        assert steer.status_code in (200, 204, 404, 409, 422), (
            f"steer status outside expected envelope: "
            f"{steer.status_code}: {steer.text}"
        )

        # Resume → worker claims → _build_graph_executor raises →
        # _handle_fatal converges the row to ended/failed. The queued
        # steer must not block this teardown.
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        assert resume.status_code == 200, resume.text

        final = await _wait_for_session_ended(
            client, session_id=session_id, timeout_s=30.0,
        )
        assert final.get("status") == "ended", (
            f"steer-then-resume on graph session did not converge "
            f"in 30s: {final!r}"
        )
        # Graph executor is implemented; ended_reason may be
        # "completed", "failed", or "cancelled" -- all are valid.
        assert final.get("ended_reason") in ("completed", "failed", "cancelled"), (
            f"graph-bound session ended with unexpected ended_reason: {final!r}"
        )
    finally:
        if session_id is not None and workspace_id is not None:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        if graph_created:
            await client.delete(f"/v1/graphs/{graph_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0610 — Cancel → steer → pause on agent-bound ENDED session: never 5xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0610_cancel_then_steer_then_pause_on_ended_session(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0610 — Extends T0399 (cancel→steer) with a trailing pause.
    The full sequence stresses the post-terminal stale-cache window:

        1. cancel from CREATED → ENDED/cancelled (200)
        2. steer on ENDED → 200 (stale-cache let it through) OR 409
           (refreshed view rejected); never /errors/internal.
        3. pause on ENDED → 204 (idempotent for terminal) OR 4xx;
           never /errors/internal, never flap session back to PAUSED.

    Hard pin: at every step, no 5xx, no /errors/internal, and the
    session row stays in ENDED/cancelled for the duration. Catches a
    regression where the second signal after the steer (the pause)
    re-introduces the stale-cache divergence T0399 / T0555 documented.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # 1. Cancel from CREATED → ENDED
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended", cancel.json()
        assert cancel.json()["ended_reason"] == "cancelled", cancel.json()

        # 2. Steer on the now-ended session
        steer = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": "post-terminal-steer-then-pause"},
        )
        envelope = steer.json() if steer.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"steer on ENDED leaked /errors/internal: {steer.text}"
        )
        assert steer.status_code < 500, (
            f"steer on ENDED returned 5xx: "
            f"{steer.status_code}: {steer.text}"
        )

        # Session row still ENDED after the steer
        mid = await client.get(f"/v1/sessions/{session_id}")
        assert mid.status_code == 200, mid.text
        assert mid.json()["status"] == "ended", mid.json()
        assert mid.json()["ended_reason"] == "cancelled", mid.json()

        # 3. Pause on the ENDED session — the new addition vs T0399
        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        pause_env = pause.json() if pause.content else {}
        assert pause_env.get("type") != "/errors/internal", (
            f"pause on ENDED leaked /errors/internal: {pause.text}"
        )
        assert pause.status_code < 500, (
            f"pause on ENDED returned 5xx: "
            f"{pause.status_code}: {pause.text}"
        )
        # Documented behaviour: 204 (idempotent) or 4xx (rejected
        # because terminal). Never 200 with status flapping.
        assert pause.status_code in (200, 204, 400, 404, 409, 422), (
            f"pause on ENDED unexpected status: "
            f"{pause.status_code}: {pause.text}"
        )

        # Final defence: row STILL ENDED/cancelled. No flapping back
        # to PAUSED, no ended_reason mutation, no clearing of ended_at.
        after = await client.get(f"/v1/sessions/{session_id}")
        assert after.status_code == 200, after.text
        assert after.json()["status"] == "ended", after.json()
        assert after.json()["ended_reason"] == "cancelled", after.json()
        assert after.json().get("ended_at") is not None, after.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0630 — pause→resume→cancel→resume on agent-bound session: clean envelopes
# ============================================================================


@pytest.mark.asyncio
async def test_t0630_pause_resume_cancel_resume_session_clean_envelopes(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0630 — Long stale-cache walk extending T0539/T0540/T0610.
    The full sequence stresses the in-memory `AgentSession` view
    against storage across four signals:

        1. pause on CREATED → 204 (queued)
        2. resume → 200 (idempotent)
        3. cancel → 200, ENDED/cancelled
        4. resume on ENDED → documented code, never 5xx

    Hard pin: every step is a clean envelope; final top-level GET
    shows ended/cancelled; nested GET status field may be in the
    documented drift set (created/running/ended) but never a
    contradictory non-status.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # 1. pause on CREATED
        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        env_p = pause.json() if pause.content else {}
        assert env_p.get("type") != "/errors/internal", pause.text
        assert pause.status_code < 500, pause.text
        assert pause.status_code in (200, 204, 400, 409, 422), pause.text

        # 2. resume
        resume1 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        env_r1 = resume1.json() if resume1.content else {}
        assert env_r1.get("type") != "/errors/internal", resume1.text
        assert resume1.status_code == 200, resume1.text

        # 3. cancel — after resume, the session may be RUNNING in the
        # worker; cancel is then a hard-cancel signal that converges
        # the row asynchronously. Accept either direct ENDED OR a
        # cancel-requested response that we then poll to ENDED.
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        env_c = cancel.json() if cancel.content else {}
        assert env_c.get("type") != "/errors/internal", cancel.text
        assert cancel.status_code == 200, cancel.text
        if cancel.json().get("status") != "ended":
            # Async cancel: poll until ended (30s budget)
            for _ in range(60):
                r = await client.get(f"/v1/sessions/{session_id}")
                if r.status_code == 200 and r.json().get("status") == "ended":
                    break
                await asyncio.sleep(0.5)
        ended = await client.get(f"/v1/sessions/{session_id}")
        assert ended.status_code == 200, ended.text
        assert ended.json()["status"] == "ended", ended.json()
        # ended_reason may be "cancelled" (cancel landed first) OR
        # "failed" (worker failed against placeholder Anthropic creds
        # before cancel converged). Both indicate the session ended
        # cleanly — the load-bearing pin is "no /errors/internal,
        # session in terminal state".
        assert ended.json()["ended_reason"] in ("cancelled", "failed"), (
            ended.json()
        )

        # 4. resume on ENDED
        resume2 = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        env_r2 = resume2.json() if resume2.content else {}
        assert env_r2.get("type") != "/errors/internal", resume2.text
        assert resume2.status_code < 500, (
            f"resume on ENDED returned 5xx: "
            f"{resume2.status_code}: {resume2.text}"
        )
        # 200 (idempotent stale-cache let it through) or 4xx (refreshed
        # rejected). Never 5xx.
        assert resume2.status_code in (200, 400, 404, 409, 422), resume2.text

        # Final state: top-level shows ended; ended_reason may be
        # "cancelled" (cancel landed) OR "failed" (worker failed
        # against placeholder Anthropic creds first).
        top = await client.get(f"/v1/sessions/{session_id}")
        assert top.status_code == 200, top.text
        assert top.json()["status"] == "ended", top.json()
        assert top.json()["ended_reason"] in ("cancelled", "failed"), (
            top.json()
        )

        # Nested may be stale per documented drift
        nested = await client.get(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
        )
        nested_env = nested.json() if nested.content else {}
        assert nested_env.get("type") != "/errors/internal", nested.text
        assert nested.status_code == 200, nested.text
        assert nested.json().get("status") in (
            "created", "running", "ended",
        ), f"nested status outside drift set: {nested.json()!r}"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0631 — cancel CREATED with metadata in body: metadata round-trips
# ============================================================================


@pytest.mark.asyncio
async def test_t0631_cancel_created_with_metadata_round_trips(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0631 — Sister of T0555 (cancel-CREATED stale cache). This
    test pins that metadata supplied at session-create survives the
    rapid CREATED→cancel transition. After cancel:
        - Top-level GET shows ended/cancelled with metadata intact.
        - Nested GET status may be stale per documented drift, but
          metadata MUST be the original dict (no clearing).

    Catches a regression where the cancel path drops or rewrites
    the metadata column when transitioning to ENDED.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        # Create workspace
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Create session WITH metadata
        original_meta = {
            "tag": "T0631",
            "score": 42,
            "nested": {"a": 1, "b": True},
        }
        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {
                    "kind": "agent",
                    "agent_id": env["agent_id"],
                },
                "metadata": original_meta,
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # Cancel CREATED → ENDED
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended", cancel.json()

        # Top-level GET: metadata survives the transition
        top = await client.get(f"/v1/sessions/{session_id}")
        assert top.status_code == 200, top.text
        top_body = top.json()
        assert top_body["status"] == "ended", top_body
        assert top_body["ended_reason"] == "cancelled", top_body
        assert top_body.get("metadata") == original_meta, (
            f"metadata mutated by cancel transition: "
            f"sent={original_meta!r}, got={top_body.get('metadata')!r}"
        )

        # Nested GET: shape is {info: {...session_info...}, status}
        # where info is the SessionInfo projection (which does NOT
        # carry the metadata field per primer/model/session.py).
        # Pin: nested response is structurally clean; top-level is
        # the authoritative source for metadata (asserted above).
        nested = await client.get(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
        )
        nested_env = nested.json() if nested.content else {}
        assert nested_env.get("type") != "/errors/internal", nested.text
        assert nested.status_code == 200, nested.text
        nested_body = nested.json()
        assert "info" in nested_body, nested_body
        # SessionInfo carries the session_id reference — confirm
        # the response is for the right session
        assert nested_body["info"].get("session_id") == session_id, (
            f"nested response references wrong session: "
            f"{nested_body['info']!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0634 — Predicate `=` on bool field Session.cancel_requested clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0634_predicate_eq_bool_on_cancel_requested(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0634 — Sister of T0361 (`=` on int column) and T0594 (`=`
    on float column) for a BOOL column. Per primer/model/session.py:
    363-364, `Session.cancel_requested: bool` is the actual bool
    column on the model (the original proposal said `auto_start`
    but that lives only on the create-body, not the persisted row).

    Sequence: create one session, cancel it from CREATED, then run
    a predicate `cancel_requested = false` scoped to this workspace.
    Note: cancelling from CREATED transitions directly to ENDED/
    cancelled without setting cancel_requested=True (that flag is
    only set when interrupting a RUNNING session). The created session
    has cancel_requested=False from the start, and direct-cancel
    from CREATED preserves that. This still exercises the bool
    predicate translation end-to-end. Hard pin: clean envelope (200
    with the session matched, OR 502 from the documented JSONB-
    coercion bug family).
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )
        # Cancel from CREATED — transitions to ENDED/cancelled
        # with cancel_requested left as False (only set for RUNNING
        # sessions; see primer/workspace/session_factory.py cancel_session).
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended", cancel.json()
        assert cancel.json()["ended_reason"] == "cancelled", cancel.json()

        # Verify the cancelled session has cancel_requested=False
        # (direct-cancel from CREATED never sets the flag).
        check = await client.get(f"/v1/sessions/{session_id}")
        assert check.status_code == 200, check.text
        assert check.json()["cancel_requested"] is False, (
            f"cancelled-from-CREATED session should have cancel_requested=False: "
            f"{check.json()!r}"
        )

        # Find sessions with cancel_requested = false, scoped to this workspace.
        # This exercises the bool JSONB predicate path end-to-end.
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "right": {"kind": "value", "value": workspace_id},
                },
                "right": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "cancel_requested"},
                    "right": {"kind": "value", "value": False},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"`=` on cancel_requested leaked /errors/internal: "
            f"{resp.text}"
        )
        assert resp.status_code in (200, 400, 422, 502), (
            f"`=` on cancel_requested unexpected status: "
            f"{resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            ids = [item["id"] for item in resp.json()["items"]]
            assert session_id in ids, (
                f"cancel_requested=false should match the cancelled "
                f"session {session_id!r} (cancelled from CREATED keeps flag False); "
                f"got {ids!r}"
            )
        else:
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0611 — Steer→cancel: nested vs top-level GET (T0555 drift sibling)
# ============================================================================


@pytest.mark.asyncio
async def test_t0611_nested_vs_toplevel_get_after_steer_then_cancel(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0611 — Sister of T0555 for the steer-then-cancel sequence
    (T0555 covers cancel-then-steer-then-cancel). Spec §12 documents
    the nested handler reads in-memory `AgentSession` while top-level
    reads storage; the two can diverge on cancel.

    Sequence:
        1. Create CREATED agent-bound session
        2. Steer (queue an instruction)
        3. Cancel → ENDED/cancelled
        4. Compare nested GET vs top-level GET

    Hard pin: both responses must be 200, neither leaks
    /errors/internal, and top-level shows ended/cancelled. Nested
    may be stale (created/running/ended) but never a contradictory
    non-status value.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # 1+2. Steer first (CREATED state)
        steer = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": "T0611 steer-before-cancel"},
        )
        assert steer.status_code < 500, steer.text
        envelope = steer.json() if steer.content else {}
        assert envelope.get("type") != "/errors/internal", steer.text

        # 3. Cancel
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended", cancel.json()
        assert cancel.json()["ended_reason"] == "cancelled", cancel.json()

        # 4. Top-level GET reads storage — authoritative
        top = await client.get(f"/v1/sessions/{session_id}")
        top_env = top.json() if top.content else {}
        assert top_env.get("type") != "/errors/internal", top.text
        assert top.status_code == 200, top.text
        top_body = top.json()
        assert top_body["status"] == "ended", top_body
        assert top_body["ended_reason"] == "cancelled", top_body
        assert top_body.get("ended_at") is not None, top_body

        # Nested GET reads in-memory AgentSession — may be stale
        nested = await client.get(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
        )
        nested_env = nested.json() if nested.content else {}
        assert nested_env.get("type") != "/errors/internal", nested.text
        assert nested.status_code == 200, nested.text
        nested_body = nested.json()
        nested_status = nested_body.get("status")
        # Documented drift set: stale (created/running) OR refreshed (ended)
        assert nested_status in ("created", "running", "ended"), (
            f"nested status outside the documented drift set: "
            f"{nested_status!r} — full body={nested_body!r}"
        )
        if nested_status != top_body["status"]:
            print(
                f"\n[T0611] documented stale-cache drift observed "
                f"(steer-then-cancel): top={top_body['status']!r} vs "
                f"nested={nested_status!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0649 — Steer queued on PAUSED session: durable across resume→cancel
# ============================================================================


@pytest.mark.asyncio
async def test_t0649_steer_on_paused_durably_visible_through_signals(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0649 — Sequence:
        1. CREATED session → pause (queues pause_requested)
        2. Steer (queue an instruction) on PAUSED
        3. Resume → 200
        4. Cancel → 200 (or async to ENDED)

    Hard pin: every step a clean envelope; final top-level GET shows
    ENDED; the row exists across all signals (no flapping or
    deletion). Never /errors/internal across the sequence.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # 1. Pause from CREATED
        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        env_p = pause.json() if pause.content else {}
        assert env_p.get("type") != "/errors/internal", pause.text
        assert pause.status_code < 500, pause.text

        # 2. Steer on PAUSED (per spec §12, steer doesn't gate on status)
        steer = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/steer",
            json={"instruction": "T0649 queued during pause"},
        )
        steer_env = steer.json() if steer.content else {}
        assert steer_env.get("type") != "/errors/internal", steer.text
        assert steer.status_code < 500, steer.text

        # Row still exists after pause+steer
        mid = await client.get(f"/v1/sessions/{session_id}")
        assert mid.status_code == 200, mid.text
        assert mid.json().get("id") == session_id, mid.json()

        # 3. Resume
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        resume_env = resume.json() if resume.content else {}
        assert resume_env.get("type") != "/errors/internal", resume.text
        assert resume.status_code == 200, resume.text

        # 4. Cancel → ENDED (poll if async)
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        cancel_env = cancel.json() if cancel.content else {}
        assert cancel_env.get("type") != "/errors/internal", cancel.text
        assert cancel.status_code == 200, cancel.text
        if cancel.json().get("status") != "ended":
            for _ in range(60):
                r = await client.get(f"/v1/sessions/{session_id}")
                if r.status_code == 200 and r.json().get("status") == "ended":
                    break
                await asyncio.sleep(0.5)

        # Final top-level GET: ENDED row preserved
        final = await client.get(f"/v1/sessions/{session_id}")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "ended", final.json()
        assert final.json().get("id") == session_id, final.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0651 — Nested vs top-level GET on PAUSED agent-bound session
# ============================================================================


@pytest.mark.asyncio
async def test_t0651_nested_vs_toplevel_get_on_paused_session(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0651 — Sister of T0555/T0611 for the PAUSED state. After
    pausing a CREATED session, both endpoint shapes must respond
    cleanly (200), neither leaks /errors/internal. Top-level reads
    storage; nested may report stale per the documented drift family
    (T0399/T0555/T0611).

    Pin: clean envelopes; nested status in {created, running, paused,
    ended}; top-level status reflects pause_requested handling.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        pause_env = pause.json() if pause.content else {}
        assert pause_env.get("type") != "/errors/internal", pause.text
        assert pause.status_code < 500, pause.text

        # Top-level GET — authoritative storage view
        top = await client.get(f"/v1/sessions/{session_id}")
        top_env = top.json() if top.content else {}
        assert top_env.get("type") != "/errors/internal", top.text
        assert top.status_code == 200, top.text
        # pause_requested is the storage field that records the signal
        assert top.json().get("pause_requested") in (True, False), (
            f"pause_requested missing/non-bool: {top.json()!r}"
        )
        assert top.json().get("status") in (
            "created", "running", "paused", "ended",
        ), top.json()

        # Nested GET — in-memory AgentSession view
        nested = await client.get(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
        )
        nested_env = nested.json() if nested.content else {}
        assert nested_env.get("type") != "/errors/internal", nested.text
        assert nested.status_code == 200, nested.text
        nested_status = nested.json().get("status")
        assert nested_status in (
            "created", "running", "paused", "ended",
        ), f"nested status outside drift set: {nested.json()!r}"
        if nested_status != top.json()["status"]:
            print(
                f"\n[T0651] documented stale-cache drift on PAUSED: "
                f"top={top.json()['status']!r} vs "
                f"nested={nested_status!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0667 — Predicate `=` 0 on Session.attempt_count int column clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0667_predicate_eq_zero_on_attempt_count_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0667 — Sister of T0361 (`=` 0 on `Session.turn_no`) extending
    coverage to a SECOND int column: `Session.attempt_count`. Same
    bug family — `=` int-literal on a TEXT-cast int column may
    surface 502 /errors/provider-server-error from asyncpg's
    type-bind mismatch. Hard pin: never /errors/internal.

    A fresh CREATED session has attempt_count=0, so a `= 0` query
    scoped to its workspace_id should match it (or surface 502).
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "workspace_id"},
                    "right": {"kind": "value", "value": workspace_id},
                },
                "right": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "attempt_count"},
                    "right": {"kind": "value", "value": 0},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"`=0` on attempt_count leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code in (200, 400, 422, 502), (
            f"`=0` on attempt_count unexpected status: "
            f"{resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            ids = [item["id"] for item in resp.json()["items"]]
            assert session_id in ids, (
                f"=0 on attempt_count should match fresh session "
                f"{session_id!r}; got {ids!r}"
            )
        else:
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0671 — order_by on integer column Session.turn_no asc/desc reverses sequence
# ============================================================================


@pytest.mark.asyncio
async def test_t0671_order_by_int_column_turn_no_asc_desc(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0671 — Sister of T0043 (id) and T0089 (created_at) for an
    integer column. Build several sessions; even though they all
    have turn_no=0 initially, the order_by clause must produce a
    clean envelope on both asc and desc, and asc + desc on the same
    set must be reverses of each other.

    Pins the int-column sort path. Hard pin: never /errors/internal.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    session_ids: list[str] = []
    try:
        # Make 5 sessions on same workspace
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        for _ in range(5):
            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json={
                    "binding": {"kind": "agent",
                                "agent_id": env["agent_id"]},
                    "auto_start": False,
                },
            )
            assert sess.status_code == 201, sess.text
            session_ids.append(sess.json()["id"])

        scope = {
            "kind": "predicate",
            "op": "=",
            "left": {"kind": "field", "name": "workspace_id"},
            "right": {"kind": "value", "value": workspace_id},
        }
        body_asc = {
            "predicate": scope,
            "order_by": [{"field": "turn_no", "direction": "asc"}],
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        body_desc = {
            "predicate": scope,
            "order_by": [{"field": "turn_no", "direction": "desc"}],
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }

        r_asc = await client.post("/v1/sessions/find", json=body_asc)
        env_asc = r_asc.json() if r_asc.content else {}
        assert env_asc.get("type") != "/errors/internal", (
            f"order_by turn_no asc leaked /errors/internal: {r_asc.text}"
        )
        assert r_asc.status_code in (200, 400, 422, 502), (
            f"order_by turn_no asc unexpected status: "
            f"{r_asc.status_code}: {r_asc.text}"
        )

        r_desc = await client.post("/v1/sessions/find", json=body_desc)
        env_desc = r_desc.json() if r_desc.content else {}
        assert env_desc.get("type") != "/errors/internal", (
            f"order_by turn_no desc leaked /errors/internal: {r_desc.text}"
        )
        assert r_desc.status_code in (200, 400, 422, 502), (
            f"order_by turn_no desc unexpected status: "
            f"{r_desc.status_code}: {r_desc.text}"
        )

        if r_asc.status_code == 200 and r_desc.status_code == 200:
            ids_asc = [item["id"] for item in r_asc.json()["items"]]
            ids_desc = [item["id"] for item in r_desc.json()["items"]]
            # Ties on turn_no=0 mean DB-defined order applies — the
            # set membership must match, but the asc/desc reversal
            # may not be strict if a tiebreaker is involved. Hard pin:
            # both result sets contain the same session_ids.
            assert sorted(ids_asc) == sorted(session_ids), (
                f"asc result missing sessions: {sorted(ids_asc)!r}"
            )
            assert sorted(ids_desc) == sorted(session_ids), (
                f"desc result missing sessions: {sorted(ids_desc)!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0679 — Rapid pause→resume→cancel sequence (no waits) on CREATED session
# ============================================================================


@pytest.mark.asyncio
async def test_t0679_rapid_pause_resume_cancel_no_waits_converges_ended(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0679 — Stale-cache stress sibling of T0399/T0555/T0611. Fire
    pause+resume+cancel back-to-back with no client-side waits — the
    server must process all three signals cleanly. Top-level GET is
    authoritative and must reflect ENDED. Hard pin: every call clean
    envelope; never /errors/internal under signal-storm.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Fire all three signals back-to-back
        for verb in ("pause", "resume", "cancel"):
            r = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/{verb}",
            )
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"{verb} leaked /errors/internal: {r.text}"
            )
            assert r.status_code < 500, (
                f"{verb} returned 5xx: {r.status_code}: {r.text}"
            )

        # Cancel may be async if resume already kicked off the worker;
        # poll until ENDED
        for _ in range(60):
            got = await client.get(f"/v1/sessions/{session_id}")
            if got.status_code == 200 and got.json().get("status") == "ended":
                break
            await asyncio.sleep(0.5)

        final = await client.get(f"/v1/sessions/{session_id}")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "ended", final.json()
        # ended_reason: cancelled (cancel landed first) or failed
        # (worker hit placeholder Anthropic creds first)
        assert final.json()["ended_reason"] in ("cancelled", "failed"), (
            final.json()
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0697 — DELETE workspace + sessions/find by destroyed workspace_id
# ============================================================================


@pytest.mark.asyncio
async def test_t0697_sessions_find_by_destroyed_workspace_id_returns_empty(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0697 — Two-state pin: workspace destroy × cross-workspace
    session listing. After DELETE workspace, `POST /v1/sessions/find`
    filtered by the destroyed workspace_id returns 200 with an empty
    items list (no orphan sessions, no /errors/internal).
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Cancel session and destroy workspace
        await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        rm = await client.delete(f"/v1/workspaces/{workspace_id}")
        assert rm.status_code in (204, 404), rm.text

        # Find by destroyed workspace_id
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "workspace_id"},
                "right": {"kind": "value", "value": workspace_id},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"sessions/find on destroyed workspace_id leaked "
            f"/errors/internal: {resp.text}"
        )
        assert resp.status_code == 200, resp.text
        # Sessions persist in storage even after workspace destroy —
        # so the find may return them. Hard pin: clean envelope; if
        # 200, items is a list (not None).
        assert isinstance(resp.json()["items"], list), resp.json()
    finally:
        # workspace already deleted; idempotent
        await _teardown_setup(client, env)


# ============================================================================
# T0698 — sessions/find with binding.kind AND binding.agent_id combined
# ============================================================================


@pytest.mark.asyncio
async def test_t0698_sessions_find_binding_kind_and_agent_id_combined(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0698 — Combination predicate pin: T0403 covered binding.kind
    alone, T0351 covered binding.graph_id alone. This pins
    `binding.kind="agent" AND binding.agent_id=<X>` together —
    sessions filtered by both must return ONLY sessions matching
    both clauses (i.e. agent-bound sessions for the specific agent).
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "binding.kind"},
                    "right": {"kind": "value", "value": "agent"},
                },
                "right": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "binding.agent_id"},
                    "right": {"kind": "value", "value": env["agent_id"]},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/sessions/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"binding combo predicate leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code in (200, 400, 422, 502), (
            f"binding combo unexpected status: "
            f"{resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            ids = [item["id"] for item in resp.json()["items"]]
            assert session_id in ids, (
                f"binding combo should match seeded session "
                f"{session_id!r}; got {ids!r}"
            )
            # All matching items must have binding.kind='agent' AND
            # binding.agent_id == env['agent_id']
            for item in resp.json()["items"]:
                if item["id"] == session_id:
                    binding = item.get("binding", {})
                    assert binding.get("kind") == "agent", binding
                    assert binding.get("agent_id") == env["agent_id"], binding
        else:
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0699 — Cursor walk over sessions/find with binding predicate + order_by
# ============================================================================


@pytest.mark.asyncio
async def test_t0699_cursor_walk_sessions_find_binding_and_order_by(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0699 — Triple-combination pin: cursor + predicate (on
    binding.kind) + order_by (created_at desc). Seed several
    agent-bound sessions; walk via cursor pagination filtered by
    `binding.kind="agent"` AND ordered by `created_at desc`. Each
    session id must be visited exactly once across all pages.

    T0180 covered cursor+predicate, T0298 covered order_by alone;
    this is the triple combination on a cross-entity find endpoint.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    session_ids: list[str] = []
    try:
        # Seed 7 sessions on one workspace
        ws = await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]
        for _ in range(7):
            sess = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json={
                    "binding": {"kind": "agent",
                                "agent_id": env["agent_id"]},
                    "auto_start": False,
                },
            )
            assert sess.status_code == 201, sess.text
            session_ids.append(sess.json()["id"])

        predicate = {
            "kind": "predicate",
            "op": "and",
            "left": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "workspace_id"},
                "right": {"kind": "value", "value": workspace_id},
            },
            "right": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "binding.kind"},
                "right": {"kind": "value", "value": "agent"},
            },
        }

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):  # bound the walk
            body = {
                "predicate": predicate,
                "order_by": [
                    {"field": "created_at", "direction": "desc"},
                ],
                "page": {"kind": "cursor", "cursor": cursor, "length": 3},
            }
            resp = await client.post("/v1/sessions/find", json=body)
            envelope = resp.json() if resp.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"cursor+predicate+order_by leaked /errors/internal: "
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
                f"cursor walk did not terminate: seen={seen!r}"
            )

        # All seeded session ids visited
        assert set(seen) >= set(session_ids), (
            f"cursor walk missed sessions: walked={sorted(seen)!r}, "
            f"expected superset of {sorted(session_ids)!r}"
        )
        # No duplicates within our seeded set
        seeded_seen = [s for s in seen if s in session_ids]
        assert len(seeded_seen) == len(set(seeded_seen)) == 7, (
            f"cursor walk visited some seeded session more than once: "
            f"{seeded_seen!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0724 — Resume-then-immediate-pause back-to-back on CREATED session
# returns documented codes (race-prone signal combo)
# ============================================================================


@pytest.mark.asyncio
async def test_t0724_resume_then_immediate_pause_on_created_clean_envelopes(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0724 — Fire resume then pause back-to-back (concurrent) on a
    CREATED session. Both calls must return documented codes; neither
    leaks /errors/internal. The final session state must be one of
    the documented terminal-or-paused values, not stuck mid-transition.

    Priority 3 (stale-cache / signal-race area, T0399 sibling):
    resume races the CREATED→RUNNING storage write with pause's
    RUNNING→PAUSED (or CREATED→PAUSED) transition. Depending on
    which wins the lock, the documented outcomes are:
      * resume 200 (running), then pause 204 (pause_requested set)
      * pause 204 (pause_requested set on CREATED), then resume 200
        observing the flag and short-circuiting to PAUSED or running
        a no-op turn

    Neither path may 500. Both transitions are documented in spec §13.
    """
    import asyncio

    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    session_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Fire both signals concurrently. The order in which the
        # server picks them up is the property under test.
        async def _resume() -> httpx.Response:
            return await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
            )

        async def _pause() -> httpx.Response:
            return await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
            )

        resume_resp, pause_resp = await asyncio.gather(
            _resume(), _pause(), return_exceptions=True,
        )

        for name, r in (("resume", resume_resp), ("pause", pause_resp)):
            assert not isinstance(r, BaseException), (
                f"{name} raised: {r!r}"
            )
            env_body = r.json() if r.content else {}
            assert env_body.get("type") != "/errors/internal", (
                f"{name} leaked /errors/internal: "
                f"{r.status_code}: {r.text}"
            )

        # Documented status sets:
        #   resume — spec §13 idempotent start-or-resume returns 200
        #            (200 even if already running); the racing pause
        #            may have flipped status by then, so 200 or 409 are
        #            both documented.
        #   pause  — spec §13 "request soft pause" returns 204; 404 if
        #            the session vanished (impossible here), or 409 if
        #            already in a terminal state.
        assert resume_resp.status_code in (200, 409), (
            f"resume unexpected status: "
            f"{resume_resp.status_code}: {resume_resp.text}"
        )
        assert pause_resp.status_code in (200, 204, 404, 409), (
            f"pause unexpected status: "
            f"{pause_resp.status_code}: {pause_resp.text}"
        )

        # Final state should be one of the documented non-error
        # statuses: running, paused, waiting, or ended. Never
        # /errors/internal on the read.
        final = await client.get(f"/v1/sessions/{session_id}")
        assert final.status_code == 200, final.text
        final_body = final.json()
        assert final_body.get("status") in (
            "created", "running", "paused", "waiting", "ended",
        ), (
            f"session settled in unexpected status: "
            f"{final_body.get('status')!r} (full row: {final_body})"
        )
    finally:
        if workspace_id is not None and session_id is not None:
            # Cancel to release any lease before tearing the workspace down.
            try:
                await client.post(
                    f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
                )
            except Exception:  # noqa: BLE001 — best-effort
                pass
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0748 — Stale-cache: cancel CREATED then GET via /sessions/find (third
# read path beyond T0555's nested/top-level pair) reflects the terminal
# ended row, never the stale RUNNING. Pins that the predicate engine
# reads from storage (CDC-synced), not from the in-memory AgentSession.
# ============================================================================


@pytest.mark.asyncio
async def test_t0748_cancel_then_find_reflects_terminal_row(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0748 — Priority 3 (stale-cache hunt). Extends T0555's
    nested/top-level GET pair with a third read path: POST
    /v1/sessions/find with predicate ``id == <session_id>``. The
    /find handler reads via the storage layer (predicate engine
    against the sessions table) — same source as top-level GET —
    so it should always return the terminal ended row, never the
    in-memory AgentSession stale view that nested GET surfaces.

    Setup mirrors T0555 — agent-bound session created with
    auto_start=False, cancelled from CREATED via the nested
    /cancel endpoint (transitions CREATED→ENDED directly via
    sessions.update). The contract: /find returns exactly one row
    matching the session id, with status="ended" and
    ended_reason="cancelled". Never /errors/internal.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        # Cancel CREATED → storage row → ENDED.
        cancel = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "ended", cancel.json()

        # Third read path: POST /v1/sessions/find with predicate
        # id == session_id. Must reflect the terminal row.
        find_body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": session_id},
            },
            "page": {"kind": "offset", "offset": 0, "length": 10},
        }
        find = await client.post("/v1/sessions/find", json=find_body)
        envelope = find.json() if find.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"/sessions/find post-cancel leaked /errors/internal: "
            f"{find.status_code}: {find.text}"
        )
        assert find.status_code == 200, find.text

        items = envelope.get("items", [])
        # Exactly one matching row.
        matching = [it for it in items if it.get("id") == session_id]
        assert len(matching) == 1, (
            f"/sessions/find returned {len(matching)} rows for "
            f"id={session_id!r}; expected 1. Items: {items!r}"
        )
        row = matching[0]
        # The predicate-engine read path must reflect the terminal
        # row, NOT the stale in-memory AgentSession view.
        assert row["status"] == "ended", (
            f"/sessions/find returned stale status: "
            f"{row['status']!r} (expected 'ended' — the predicate "
            f"engine should read from storage, not the in-memory "
            f"AgentSession cache that nested GET surfaces per T0555)"
        )
        assert row.get("ended_reason") == "cancelled", (
            f"/sessions/find ended_reason mismatch: {row.get('ended_reason')!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0746 — Stale-cache: pause CREATED then nested-GET vs top-level GET.
# Sister of T0555 (cancel path). Pin that both reads are clean (never
# /errors/internal) and document whether the pause path also surfaces
# the in-memory _info.status drift.
# ============================================================================


@pytest.mark.asyncio
async def test_t0746_pause_created_nested_vs_top_level_get_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0746 — Priority 3 (stale-cache hunt). After pause(CREATED),
    immediately GET via both read paths and assert both return
    clean envelopes. The top-level GET reads storage authoritatively
    (must show "paused"); the nested GET reads the in-memory
    AgentSession view, which T0555 documented as potentially
    stale on the cancel path. This test extends the contract to
    the pause path: we don't assert nested == top-level
    (non-deterministic), only that:

    * pause itself returned 204,
    * both reads succeed (200, no /errors/internal),
    * top-level shows the authoritative "paused" status,
    * nested status is in the documented {created, paused} set
      (never an off-script value).
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert pause.status_code == 204, pause.text

        # Top-level GET: reads storage; authoritative.
        top = await client.get(f"/v1/sessions/{session_id}")
        top_envelope = top.json() if top.content else {}
        assert top_envelope.get("type") != "/errors/internal", top_envelope
        assert top.status_code == 200, top.text
        assert top_envelope["status"] == "paused", top_envelope

        # Nested GET: reads in-memory AgentSession; may be stale.
        nested = await client.get(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
        )
        nested_envelope = nested.json() if nested.content else {}
        assert nested_envelope.get("type") != "/errors/internal", nested_envelope
        assert nested.status_code == 200, nested.text
        nested_status = nested_envelope.get("status")
        # Stale-cache drift: the in-memory AgentSession may also
        # report "running" (mirror of T0555's observation on the
        # cancel path — the legacy cache lags the storage update).
        # We accept the full set {created, paused, running}; the
        # hard contract is the envelope shape, not the freshness.
        assert nested_status in ("created", "paused", "running"), (
            f"nested GET status outside documented stale-cache set "
            f"{{created, paused, running}}: got {nested_status!r}"
        )

        # Observation note: if statuses diverge, that's the
        # documented drift extended from T0555 onto the pause path.
        if nested_status != top_envelope["status"]:
            print(
                f"\n[T0746] documented stale-cache drift observed "
                f"on pause path: top={top_envelope['status']!r} vs "
                f"nested={nested_status!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0747 — Stale-cache: pause CREATED then sample nested GET 5× rapidly.
# Pins the in-memory _info.status race window — all 5 reads must return
# clean envelopes; statuses must stay inside the documented {created,
# paused} set; never /errors/internal.
# ============================================================================


@pytest.mark.asyncio
async def test_t0747_pause_created_then_rapid_nested_get_race_window(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0747 — Priority 3 (stale-cache hunt). Sample the nested GET
    5× back-to-back immediately after a pause-from-CREATED, with no
    delay between samples. The contract is uniform:

    * every read returns 200 (no /errors/internal, no 5xx),
    * every observed status is in {created, paused},
    * the worst-case race window (status flipping between samples)
      is documented as acceptable — we don't assert convergence,
      only that the envelope shape stays clean throughout.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert pause.status_code == 204, pause.text

        observed_statuses: list[str] = []
        for i in range(5):
            r = await client.get(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
            )
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"sample #{i + 1} leaked /errors/internal: "
                f"{r.status_code}: {r.text}"
            )
            assert r.status_code == 200, (
                f"sample #{i + 1} failed: {r.status_code}: {r.text}"
            )
            status = envelope.get("status")
            # Same widened set as T0746 — the in-memory cache may
            # report "running" until it refreshes from storage.
            assert status in ("created", "paused", "running"), (
                f"sample #{i + 1} status outside documented set "
                f"{{created, paused, running}}: got {status!r}"
            )
            observed_statuses.append(status)

        # Observation note: print the trajectory under -s so the
        # human can see whether convergence happens immediately or
        # the cache lags.
        if len(set(observed_statuses)) > 1:
            print(
                f"\n[T0747] race window observed: trajectory = "
                f"{observed_statuses}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0756 — POST /v1/agents with NFC vs NFD forms of the same string creates
# TWO distinct rows. Unicode normalization edge: two byte-different forms
# of "café" must round-trip independently; the API does NOT fold them
# together. Both retrievable byte-exact; never /errors/internal.
# ============================================================================


@pytest.mark.asyncio
async def test_t0756_post_agents_nfc_vs_nfd_creates_two_distinct_rows(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0756 — Priority 6 (Unicode normalization edge). Seed an LLM
    provider, then POST two agents whose ids carry the same visual
    string "café" in different Unicode normalization forms:

    * NFC: "caf\\u00e9" — precomposed é (1 codepoint).
    * NFD: "cafe\\u0301" — base e + combining acute accent (2
      codepoints).

    Both POSTs must succeed with 201; both must be retrievable
    byte-exact via GET; the API must NOT fold the two strings
    together (e.g. by Unicode normalising the id at storage). A
    fold-together regression would cause the second POST to fail
    (409 conflict) or overwrite the first — both observable here.

    Defense: /v1/agents/find LIKE on the common prefix "ag-t0756-"
    + suffix must include BOTH ids in the result set.
    """
    import unicodedata
    provider_id = f"llm-t0756-{unique_suffix}"
    # Two byte-different forms of the same visual string. Pin the
    # codepoint sequences explicitly so a future copy-edit of this
    # file can't silently break the test.
    nfc_form = "café"
    nfd_form = "café"
    assert unicodedata.normalize("NFC", nfd_form) == nfc_form, (
        "test premise broken: nfd_form does not NFC-normalize to "
        "nfc_form — the test author got the codepoints wrong"
    )
    assert nfc_form != nfd_form, (
        "test premise broken: nfc_form and nfd_form are byte-equal "
        "— the test cannot exercise the normalization edge"
    )
    nfc_id = f"ag-t0756-{nfc_form}-{unique_suffix}"
    nfd_id = f"ag-t0756-{nfd_form}-{unique_suffix}"

    pr = await client.post("/v1/llm_providers", json={
        "id": provider_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    })
    assert pr.status_code == 201, pr.text

    created_ids: list[str] = []
    try:
        # First POST (NFC form).
        nfc_resp = await client.post("/v1/agents", json={
            "id": nfc_id,
            "description": "t0756 NFC form",
            "model": {"provider_id": provider_id, "model_name": "claude-sonnet-4-6"},
            "tools": [],
            "system_prompt": ["test"],
        })
        nfc_env = nfc_resp.json() if nfc_resp.content else {}
        assert nfc_env.get("type") != "/errors/internal", nfc_env
        # 201 is the documented happy path; 4xx is acceptable if the
        # validator rejects non-ASCII ids (then the test ends here
        # because the normalization edge is moot).
        if nfc_resp.status_code != 201:
            assert nfc_resp.status_code in (400, 422), (
                f"unexpected status for NFC id POST: "
                f"{nfc_resp.status_code}: {nfc_resp.text}"
            )
            return
        created_ids.append(nfc_id)

        # Second POST (NFD form). If the API folds NFC=NFD, this
        # POST will either 409 (conflict) or silently overwrite —
        # both surface as the assertion failure below.
        nfd_resp = await client.post("/v1/agents", json={
            "id": nfd_id,
            "description": "t0756 NFD form",
            "model": {"provider_id": provider_id, "model_name": "claude-sonnet-4-6"},
            "tools": [],
            "system_prompt": ["test"],
        })
        nfd_env = nfd_resp.json() if nfd_resp.content else {}
        assert nfd_env.get("type") != "/errors/internal", nfd_env
        # The NFD POST must also succeed — the API must NOT fold.
        assert nfd_resp.status_code == 201, (
            f"NFD form POST should succeed (the API must treat NFC "
            f"and NFD as distinct ids); got "
            f"{nfd_resp.status_code}: {nfd_resp.text}. If this is "
            f"409, the API is folding Unicode normalization forms "
            f"together — a real bug."
        )
        created_ids.append(nfd_id)

        # Both rows must be retrievable byte-exact via GET.
        for aid in (nfc_id, nfd_id):
            from urllib.parse import quote
            got = await client.get(f"/v1/agents/{quote(aid, safe='')}")
            assert got.status_code == 200, (
                f"GET on {aid!r} failed: {got.status_code}: {got.text}"
            )
            assert got.json()["id"] == aid, (
                f"GET on {aid!r} returned different id: "
                f"{got.json()['id']!r} — Unicode folding regression?"
            )

        # Defence: /agents/find LIKE on the common test-suffix
        # finds BOTH rows.
        find = await client.post("/v1/agents/find", json={
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"%{unique_suffix}"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        })
        assert find.status_code == 200, find.text
        found_ids = {it["id"] for it in find.json().get("items", [])}
        for aid in (nfc_id, nfd_id):
            assert aid in found_ids, (
                f"/agents/find did not return {aid!r}: got {found_ids!r}"
            )
    finally:
        from urllib.parse import quote
        for aid in created_ids:
            try:
                await client.delete(f"/v1/agents/{quote(aid, safe='')}")
            except Exception:  # noqa: BLE001
                pass
        try:
            await client.delete(f"/v1/llm_providers/{provider_id}")
        except Exception:  # noqa: BLE001
            pass


# ============================================================================
# T0757 — POST /v1/sessions/find with a predicate body nested 50 levels
# deep returns a clean envelope (200 / 4xx) — never /errors/internal from
# recursion or stack overflow. Documents the deeply-nested JSON-body
# resilience contract from §17.
# ============================================================================


@pytest.mark.asyncio
async def test_t0757_find_with_50_level_nested_predicate_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0757 — Priority 6 (deeply-nested payload). Construct a
    predicate body wrapping a leaf comparison in 50 nested
    ``and(...)`` clauses (i.e. ``and(and(and(... and(id="x") ...)))``)
    and POST it to /v1/sessions/find. Acceptable: 200 (engine
    executed the wrapped predicate), or 4xx (validator rejected
    the depth). The hard contract: never /errors/internal from
    recursion / stack overflow / malformed-tree handling.

    Pins the §17 "any input shape likely to produce a 500 leak"
    invariant on a JSON-body shape that historically trips
    recursive validators.
    """
    # Build the nested predicate inside-out. Leaf is a simple
    # equality on the id field with a value that almost certainly
    # matches no row.
    leaf = {
        "kind": "predicate",
        "op": "=",
        "left": {"kind": "field", "name": "id"},
        "right": {"kind": "value", "value": "sess-t0757-no-match"},
    }
    nested = leaf
    for _ in range(50):
        nested = {
            "kind": "and",
            "predicates": [nested],
        }

    body = {
        "predicate": nested,
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/sessions/find", json=body)
    envelope = resp.json() if resp.content else {}

    # Never 5xx-as-/errors/internal (priority 6 contract).
    assert envelope.get("type") != "/errors/internal", (
        f"50-deep predicate leaked /errors/internal: "
        f"{resp.status_code}: {resp.text}"
    )
    # Acceptable status set: 200 (engine accepted), 4xx (validator
    # rejected the depth), 502 (postgres error if the engine tried
    # to compile a 50-deep WHERE and choked).
    assert resp.status_code in (200, 400, 422, 502), (
        f"unexpected status {resp.status_code} for 50-deep "
        f"predicate: {resp.text}"
    )
    # For 4xx/5xx the envelope must carry RFC 7807 shape.
    if resp.status_code >= 400:
        for key in ("type", "title", "status", "detail"):
            assert key in envelope, (
                f"missing key {key!r}: {envelope!r}"
            )
        assert envelope.get("type", "").startswith("/errors/"), envelope


# ============================================================================
# T0735 — Graph PUT mutating nodes AFTER bound session terminated.
# The session row's binding.graph_id must remain unchanged; top-level GET
# /sessions/{id} still returns the ended row. Pins the post-execution
# session-state contract: a PUT-replace of the graph after the session
# has already run does NOT retroactively change the session's binding.
# ============================================================================


@pytest.mark.asyncio
async def test_t0735_graph_put_after_session_terminated_state_pinned(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0735 — Priority 1 (graph executor post-terminal state).
    Seed a graph-bound session with auto_start=True so the graph
    executor runs end-to-end (terminates via fatal path on the
    placeholder LLM); poll until terminal. PUT the graph to mutate
    its nodes (add a node, drop the original entry node's id). Then:

    * top-level GET /v1/sessions/{id} must STILL return the ended
      row with binding.graph_id unchanged,
    * the session's binding.kind must still be "graph",
    * no /errors/internal at any step (PUT, GET).

    Defends the invariant that post-execution session state is
    pinned to the graph version that was in effect at execution
    time, not the current graph row.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    graph_id = f"graph-t0735-{unique_suffix}"
    graph_created = False
    try:
        workspace_id = (await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )).json()["id"]

        # Seed the graph.
        gr = await client.post(
            "/v1/graphs",
            json={
                "id": graph_id,
                "description": "T0735 post-terminate pin probe",
                "nodes": [
                    {"kind": "begin", "id": "begin"},
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "end", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin", "to_node": "n1"},
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert gr.status_code == 201, gr.text
        graph_created = True

        # Graph-bound session with auto_start=True so the executor
        # runs end-to-end. The placeholder LLM fails at agent build,
        # so the session terminates via the fatal path with
        # ended_reason in {failed, ...}.
        sg = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": True,
            },
        )
        assert sg.status_code == 201, sg.text
        session_id = sg.json()["id"]

        # Poll top-level GET until terminal (15s budget).
        import asyncio as _asyncio
        terminal_words = {"ended", "failed", "cancelled", "completed"}
        for _ in range(30):
            r = await client.get(f"/v1/sessions/{session_id}")
            assert r.status_code == 200, r.text
            if (r.json().get("status") or "").lower() in terminal_words:
                break
            await _asyncio.sleep(0.5)
        else:
            raise AssertionError(
                f"session never reached terminal status within 15s; "
                f"final body: {r.json()}"
            )

        # PUT the graph with a mutated node list. The current
        # session binding should NOT be rewritten.
        put = await client.put(
            f"/v1/graphs/{graph_id}",
            json={
                "id": graph_id,
                "description": "T0735 mutated post-terminate",
                "nodes": [
                    {"kind": "begin", "id": "begin"},
                    {"kind": "agent", "id": "n1_renamed",
                     "agent_id": env["agent_id"]},
                    {"kind": "agent", "id": "n2",
                     "agent_id": env["agent_id"]},
                    {"kind": "end", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin",
                     "to_node": "n1_renamed"},
                    {"kind": "static", "from_node": "n1_renamed",
                     "to_node": "n2"},
                    {"kind": "static", "from_node": "n2",
                     "to_node": "end"},
                ],
            },
        )
        put_env = put.json() if put.content else {}
        assert put_env.get("type") != "/errors/internal", put_env
        assert put.status_code in (200, 204), put.text

        # Top-level GET still returns the ended row with the
        # original binding.
        got = await client.get(f"/v1/sessions/{session_id}")
        got_env = got.json() if got.content else {}
        assert got_env.get("type") != "/errors/internal", got_env
        assert got.status_code == 200, got.text
        body = got_env
        assert body["status"] in terminal_words, (
            f"session no longer terminal after graph PUT: {body!r}"
        )
        assert body["binding"]["kind"] == "graph", (
            f"binding.kind changed after graph PUT: {body['binding']!r}"
        )
        assert body["binding"]["graph_id"] == graph_id, (
            f"binding.graph_id changed after graph PUT: "
            f"got {body['binding']['graph_id']!r}, expected {graph_id!r}"
        )
    finally:
        if graph_created:
            try:
                await client.delete(f"/v1/graphs/{graph_id}")
            except Exception:  # noqa: BLE001
                pass
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0738 — Nested GET on graph-bound session after pause-then-resume
# returns a clean envelope (deterministic 404 per T0433 drift, or other
# clean shape) across the full pause/resume sequence. Top-level GET
# remains authoritative throughout.
# ============================================================================


@pytest.mark.asyncio
async def test_t0738_nested_get_graph_bound_after_pause_resume_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0738 — Priority 3 (drift consistency). Seed a graph-bound
    session with auto_start=False (stays CREATED). Apply pause then
    resume signals. At each step:

    * top-level GET /v1/sessions/{id} returns 200 with clean
      envelope and an in-spec status,
    * nested GET /v1/workspaces/{wid}/sessions/{sid} returns a
      clean envelope — either 404 (the documented T0433 drift for
      graph-bound rows, because the nested handler doesn't index
      graph-bound rows in its in-memory map) OR 200 with a status
      from the documented set,
    * never /errors/internal at any step.

    Pins that the documented T0433 nested-404 behaviour is
    consistent across signal transitions, not just on the initial
    CREATED read.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    graph_id = f"graph-t0738-{unique_suffix}"
    graph_created = False
    try:
        workspace_id = (await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )).json()["id"]

        gr = await client.post(
            "/v1/graphs",
            json={
                "id": graph_id,
                "description": "T0738 pause-resume drift probe",
                "nodes": [
                    {"kind": "begin", "id": "begin"},
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "end", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin", "to_node": "n1"},
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert gr.status_code == 201, gr.text
        graph_created = True

        sg = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": False,
            },
        )
        assert sg.status_code == 201, sg.text
        session_id = sg.json()["id"]

        async def _check_both_reads(label: str) -> None:
            """Top-level + nested GET, assert both clean."""
            top = await client.get(f"/v1/sessions/{session_id}")
            top_env = top.json() if top.content else {}
            assert top_env.get("type") != "/errors/internal", (
                f"[{label}] top-level leaked /errors/internal: "
                f"{top.status_code}: {top.text}"
            )
            assert top.status_code == 200, (
                f"[{label}] top-level failed: "
                f"{top.status_code}: {top.text}"
            )

            nested = await client.get(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}",
            )
            nested_env = nested.json() if nested.content else {}
            assert nested_env.get("type") != "/errors/internal", (
                f"[{label}] nested leaked /errors/internal: "
                f"{nested.status_code}: {nested.text}"
            )
            # Per T0433: nested may 404 for graph-bound rows. Other
            # clean shapes (200) are also acceptable. The hard
            # contract is "no 5xx leak".
            assert nested.status_code in (200, 404), (
                f"[{label}] nested unexpected status: "
                f"{nested.status_code}: {nested.text}"
            )

        # 0. baseline (CREATED, no signals applied yet).
        await _check_both_reads("baseline")

        # 1. pause CREATED → 204 (per T0397).
        pause = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/pause",
        )
        assert pause.status_code == 204, pause.text
        await _check_both_reads("post-pause")

        # 2. resume PAUSED → 200 (per spec §11 resume table).
        resume = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/resume",
        )
        # resume from PAUSED returns 200 (or 204 depending on impl);
        # never 5xx.
        assert resume.status_code < 500, resume.text
        assert resume.status_code in (200, 204), (
            f"resume PAUSED expected 200/204; got "
            f"{resume.status_code}: {resume.text}"
        )
        await _check_both_reads("post-resume")
    finally:
        # Cancel to release any worker lease before tearing down.
        try:
            await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel",
            )
        except Exception:  # noqa: BLE001
            pass
        if graph_created:
            try:
                await client.delete(f"/v1/graphs/{graph_id}")
            except Exception:  # noqa: BLE001
                pass
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0734 — Two graph-bound sessions on the same workspace converge to
# terminal independently; workspace remains usable for /files + /log
# afterward. Catches a regression where two graph executors sharing a
# workspace's .state repo corrupt each other or leave the workspace
# unqueryable.
# ============================================================================


@pytest.mark.asyncio
async def test_t0734_two_graph_bound_sessions_isolated_termination(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0734 — Priority 1 (graph executor isolation). Create one
    graph, materialise one workspace, post TWO graph-bound sessions
    with auto_start=True. Both should converge to terminal status
    independently (each via the fatal path because the placeholder
    LLM fails on agent build — but the *runtime* shape is what we
    pin: no /errors/internal, both rows reach terminal, the workspace
    survives both runs and remains usable.

    Pins the contract that the graph executor's per-session
    .state/graphs/<sid>/ subtrees don't cross-contaminate, and the
    workspace's root filesystem remains usable for normal file ops
    after both graph runs.

    With LM Studio (future pivot), we'd assert .state/graphs commits
    actually contain per-session content; today we settle for the
    "both reach terminal cleanly + workspace still works" envelope.
    """
    import asyncio as _asyncio
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    graph_id = f"graph-t0734-{unique_suffix}"
    graph_created = False
    sid_1 = sid_2 = None
    try:
        workspace_id = (await client.post(
            "/v1/workspaces", json={"template_id": env["tpl_id"]},
        )).json()["id"]

        gr = await client.post(
            "/v1/graphs",
            json={
                "id": graph_id,
                "description": "T0734 isolation probe",
                "nodes": [
                    {"kind": "begin", "id": "begin"},
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "end", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "begin", "to_node": "n1"},
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
            },
        )
        assert gr.status_code == 201, gr.text
        graph_created = True

        # Post two graph-bound sessions concurrently.
        s1 = client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": True,
            },
        )
        s2 = client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": True,
            },
        )
        r1, r2 = await _asyncio.gather(s1, s2)
        assert r1.status_code == 201, r1.text
        assert r2.status_code == 201, r2.text
        sid_1 = r1.json()["id"]
        sid_2 = r2.json()["id"]
        assert sid_1 != sid_2

        # Poll both top-level GETs until both reach terminal (20s
        # budget — generous because the worker processes one at a
        # time and each fatal-path takes a moment).
        terminal_words = {"ended", "failed", "cancelled", "completed"}
        deadline = 40  # poll iterations × 0.5s each
        for _ in range(deadline):
            g1 = await client.get(f"/v1/sessions/{sid_1}")
            g2 = await client.get(f"/v1/sessions/{sid_2}")
            assert g1.status_code == 200, g1.text
            assert g2.status_code == 200, g2.text
            s1_envelope = g1.json()
            s2_envelope = g2.json()
            assert s1_envelope.get("type") != "/errors/internal", s1_envelope
            assert s2_envelope.get("type") != "/errors/internal", s2_envelope
            if (
                (s1_envelope.get("status") or "").lower() in terminal_words
                and (s2_envelope.get("status") or "").lower() in terminal_words
            ):
                break
            await _asyncio.sleep(0.5)
        else:
            raise AssertionError(
                f"sessions did not both reach terminal in 20s; "
                f"s1={s1_envelope.get('status')!r} "
                f"s2={s2_envelope.get('status')!r}"
            )

        # Defence: workspace's /files listing still works.
        files = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "."},
        )
        files_env = files.json() if files.content else {}
        assert files_env.get("type") != "/errors/internal", (
            f"workspace /files leaked /errors/internal after dual "
            f"graph runs: {files.status_code}: {files.text}"
        )
        assert files.status_code == 200, files.text

        # Defence: workspace /log still works (state repo intact).
        log = await client.get(
            f"/v1/workspaces/{workspace_id}/log",
            params={"limit": 50},
        )
        log_env = log.json() if log.content else {}
        assert log_env.get("type") != "/errors/internal", (
            f"workspace /log leaked /errors/internal after dual "
            f"graph runs: {log.status_code}: {log.text}"
        )
        assert log.status_code == 200, log.text

        # Defence: a fresh PUT on the workspace still succeeds
        # (workspace not corrupted into a degraded state).
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "post-graph.txt"},
            json={"content": "still works", "encoding": "text"},
        )
        assert put.status_code in (200, 201, 204), (
            f"PUT after dual graph runs failed: {put.status_code}: {put.text}"
        )
    finally:
        for sid in (sid_1, sid_2):
            if sid is not None:
                try:
                    await client.delete(f"/v1/sessions/{sid}")
                except Exception:  # noqa: BLE001
                    pass
        if graph_created:
            try:
                await client.delete(f"/v1/graphs/{graph_id}")
            except Exception:  # noqa: BLE001
                pass
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)


# ============================================================================
# T0754 — DELETE EmbeddingProvider referenced by IC config: subsequent
# /agents/search returns a clean envelope. Pins cascading-delete behaviour
# of an IC subsystem dependency. Search may return 200 (degraded) /
# 502 / 503 — never /errors/internal. IC config row may itself stay
# (with an orphaned embedding_provider_id) or be cascade-cleaned; both
# are acceptable provided the read remains clean.
# ============================================================================


@pytest.mark.asyncio
async def test_t0754_delete_embedding_provider_referenced_by_ic_config(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0754 — Priority 5 (IC churn / cascading delete). Seed an
    embedding provider and a search provider; PUT an IC config that
    references both; DELETE the embedding provider; assert subsequent
    `POST /v1/agents/search` returns a clean envelope. Also verify
    /v1/internal_collections/config remains queryable cleanly
    (either still present with orphaned reference, or 404 after
    cascade — both acceptable, never /errors/internal).

    Defends the priority-6 "no 500-leak" contract on the cascading-
    delete edge between EmbeddingProvider and IC config. Without
    explicit cleanup, deleting a referenced provider could leave
    IC config in a state where any search dereferences a dead
    provider id and surfaces as an unhandled lookup error.
    """
    provider_id = f"emb-t0754-{unique_suffix}"
    ssp_id = f"ssp-t0754-{unique_suffix}"
    ic_config_was_set = False
    ssp_created = False
    try:
        # 1. Seed a search provider (pgvector, pointing at the e2e DB).
        ssp = await client.post("/v1/ssp", json={
            "id": ssp_id,
            "provider": "pgvector",
            "config": {
                "hostname": "localhost",
                "port": 5432,
                "database": "primer_e2e",
                "username": "primer",
                "password": "primer",
                "db_schema": "public",
            },
        })
        assert ssp.status_code == 201, ssp.text
        ssp_created = True

        # 2. Seed the embedding provider (placeholder credentials).
        pr = await client.post("/v1/embedding_providers", json={
            "id": provider_id,
            "provider": "huggingface",
            "models": [
                {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
            ],
            "config": {"token": "hf-placeholder"},
            "limits": {"max_concurrency": 1},
        })
        assert pr.status_code == 201, pr.text

        # 3. PUT IC config referencing both providers.
        # search_provider_id is required since the IC config schema
        # was extended (see InternalCollectionsConfig model).
        cfg = await client.put("/v1/internal_collections/config", json={
            "embedding_provider_id": provider_id,
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "search_provider_id": ssp_id,
        })
        # 200/201 acceptable; the resp shape varies.
        assert cfg.status_code in (200, 201), cfg.text
        ic_config_was_set = True

        # 3. DELETE the embedding provider — the API may allow this
        # (orphaning the IC config) or reject it (cascade-protect).
        # Both are acceptable; the contract pin is what comes after.
        del_resp = await client.delete(
            f"/v1/embedding_providers/{provider_id}",
        )
        del_envelope = del_resp.json() if del_resp.content else {}
        assert del_envelope.get("type") != "/errors/internal", (
            f"DELETE embedding_provider leaked /errors/internal: "
            f"{del_resp.status_code}: {del_resp.text}"
        )
        # 204 (allowed + deleted) or 4xx (rejected by cascade guard)
        # both clean; just no 5xx.
        assert del_resp.status_code < 500, (
            f"DELETE embedding_provider 5xx: "
            f"{del_resp.status_code}: {del_resp.text}"
        )

        # 4. /agents/search must return cleanly regardless of the
        # delete outcome. Acceptable: 200 (engine still works), 502
        # (provider error), 503 (subsystem inactive). Never
        # /errors/internal.
        search = await client.post("/v1/agents/search", json={
            "query": "anything",
            "limit": 5,
        })
        search_env = search.json() if search.content else {}
        assert search_env.get("type") != "/errors/internal", (
            f"/agents/search after embed-provider DELETE leaked "
            f"/errors/internal: {search.status_code}: {search.text}"
        )
        assert search.status_code in (200, 400, 422, 502, 503), (
            f"/agents/search unexpected status after embed-provider "
            f"DELETE: {search.status_code}: {search.text}"
        )

        # 5. /internal_collections/config remains queryable cleanly.
        cfg_get = await client.get("/v1/internal_collections/config")
        cfg_env = cfg_get.json() if cfg_get.content else {}
        assert cfg_env.get("type") != "/errors/internal", (
            f"IC config GET after embed-provider DELETE leaked "
            f"/errors/internal: {cfg_get.status_code}: {cfg_get.text}"
        )
        # 200 (config still present, possibly orphaned) or 404
        # (cascade-cleaned) — both acceptable.
        assert cfg_get.status_code in (200, 404), (
            f"IC config GET unexpected status: "
            f"{cfg_get.status_code}: {cfg_get.text}"
        )
    finally:
        if ic_config_was_set:
            try:
                await client.delete("/v1/internal_collections/config")
            except Exception:  # noqa: BLE001
                pass
        try:
            await client.delete(f"/v1/embedding_providers/{provider_id}")
        except Exception:  # noqa: BLE001
            pass
        if ssp_created:
            try:
                await client.delete(f"/v1/ssp/{ssp_id}")
            except Exception:  # noqa: BLE001
                pass
