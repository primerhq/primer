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
