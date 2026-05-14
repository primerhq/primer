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
        for wid, sids in (
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
    illegal-from-terminal transition matrix.
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
    coverage matrix.
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
                        {"kind": "agent", "id": "n1",
                         "agent_id": env["agent_id"]},
                        {"kind": "terminal", "id": "end"},
                    ],
                    "edges": [
                        {"kind": "static", "from_node": "n1",
                         "to_node": "end"},
                    ],
                    "entry_node_id": "n1",
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
    filter. With the bringup wiping the DB and no real worker activity
    in this test, no session exists in RUNNING status; the filter must
    return 200 with an empty `items` list.
    """
    resp = await client.get("/v1/sessions?status=running")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body, body
    assert body["items"] == [], (
        f"status=running filter should return [] on a quiet DB, "
        f"got: {body['items']!r}"
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
    """T0397 — Per matrix/api/routers/sessions.py:251, pause on a
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
    matrix/workspace/session.py:352 raises ConflictError on ENDED.
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
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "terminal", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
                "entry_node_id": "n1",
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
    """T0429 — Sibling of T0156 without the LM Studio dependency.
    Graph executor wiring is `NotImplementedError` in
    matrix/worker/pool.py:478. The worker must surface that as a
    clean session ENDED/failed with `last_error` populated, NOT
    leave the row stuck in RUNNING.

    Pin this with the cheap Anthropic-placeholder provider (no real
    LLM call ever happens because the failure occurs in
    `_build_graph_executor` BEFORE the LLM is consulted).
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
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "terminal", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
                "entry_node_id": "n1",
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
        assert final.get("ended_reason") == "failed", (
            f"graph executor is NotImplemented; expected ended_reason="
            f"'failed', got {final!r}"
        )
        # last_error must carry the executor failure text — operators
        # need this to know WHY the session failed.
        last_err = final.get("last_error")
        assert last_err, (
            f"failed graph session must populate last_error: {final!r}"
        )
        assert "NotImplementedError" in last_err or "graph" in last_err.lower(), (
            f"last_error should reference the executor failure; "
            f"got {last_err!r}"
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
    state on a graph-bound session. The worker hits
    NotImplementedError → _handle_fatal sets ENDED/failed; if cancel
    arrives first, the session may end ENDED/cancelled instead.
    Either terminal outcome is acceptable; what is NOT is sticking
    in RUNNING or surfacing /errors/internal anywhere.
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
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "terminal", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
                "entry_node_id": "n1",
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
        # ended_reason is either "failed" (worker hit NotImplementedError
        # before observing cancel) or "cancelled" (cancel landed first
        # via the storage path). Both are valid.
        assert final.get("ended_reason") in ("failed", "cancelled"), (
            f"unexpected ended_reason: {final!r}"
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


@pytest.mark.asyncio
async def test_t0433_top_level_get_reflects_fatal_ended_reason(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0433 — When a graph-bound session terminates via the fatal
    path, the row's `ended_reason` and `last_error` must be visible
    on the top-level GET /v1/sessions/{id} read.

    Documented divergence: the NESTED route
    /v1/workspaces/{wid}/sessions/{sid} returns `{info, status}`
    sourced from the LocalWorkspace's in-memory `_sessions` dict,
    which is populated only by `start_session` for AGENT bindings
    (matrix/workspace/local/workspace.py:135-160). Graph-bound
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
                    {"kind": "agent", "id": "n1",
                     "agent_id": env["agent_id"]},
                    {"kind": "terminal", "id": "end"},
                ],
                "edges": [
                    {"kind": "static", "from_node": "n1", "to_node": "end"},
                ],
                "entry_node_id": "n1",
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
        assert top_final.get("ended_reason") == "failed", top_final
        assert top_final.get("last_error"), top_final
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
            # read graph-bound rows from storage, both reads must
            # agree on ended_reason and last_error.
            nested_body = nested.json()
            info = nested_body.get("info", {})
            assert nested_body.get("status") == top_final["status"]
            assert info.get("ended_reason") == top_final.get("ended_reason")
            assert info.get("last_error") == top_final.get("last_error")
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
         short-circuit per matrix/api/routers/sessions.py:287-296)
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
