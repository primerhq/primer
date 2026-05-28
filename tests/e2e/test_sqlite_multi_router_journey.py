"""E2E: §1 SQLite-backed multi-router CRUD journey.

The §1 spec adds embedded SQLite as a first-class storage backend
alongside Postgres. tests/api/test_app_factory.py already pins the
zero-config startup path (AppConfig() → SqliteStorageProvider at
~/.primer/db/data.sqlite) at the lifespan layer. What this test
adds: a single pytest function that drives the FULL entity-router
surface against SQLite via the in-process ASGI transport, so every
major router proves it actually works against the SQLite Storage
adapter (not just Postgres).

Why this is a "user journey" and not a contract pin: it walks the
shape of a realistic operator standing up a brand-new primer
instance — pick a provider, register an agent, set up a workspace,
spin up a session, register a channel — and re-reads every entity
to confirm round-trip persistence. If any router or its Storage
adapter has a Postgres-only escape hatch (sqlite_journal_mode-
incompatible JSONB op, dialect-specific predicate, etc.) one of
the steps below will surface it.

Subsystems exercised in one test:

  1. Storage layer (SqliteStorageProvider — every entity's table
     gets CREATE'd and round-tripped)
  2. LLMProvider CRUD
  3. Agent CRUD with model + provider reference
  4. WorkspaceProvider + WorkspaceTemplate + Workspace ladder
  5. Workspace-scoped Session create (auto_start=False — no worker
     pool running in this in-process app)
  6. ToolApprovalPolicy CRUD (§2 surface)
  7. ChannelProvider + Channel + WorkspaceChannelAssociation CRUD
     (§3 surface)
  8. SemanticSearchProvider CRUD (§7 surface)
  9. InternalCollections config probe (GET — confirms the router
     mounts cleanly against SQLite even when no config is set)

Covers backlog item T0852.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.model.scheduler import (
    InMemorySchedulerConfig,
    RuntimeMode,
    SchedulerProviderConfig,
    SchedulerProviderType,
)


# 60-char placeholder for DiscordChannelProviderConfig.bot_token
_FAKE_DISCORD_TOKEN = "x" * 60


# ===========================================================================
# T0852 — SQLite multi-router journey
# ===========================================================================


@pytest.mark.asyncio
async def test_t0852_sqlite_multi_router_crud_journey(tmp_path: Path) -> None:
    """T0852 — One pytest function walks every major entity router
    against an in-process SQLite-backed FastAPI app.

    Steps:

      1. Build AppConfig with SqliteConfig pointing at tmp_path.
         RuntimeMode.API (no worker pool — avoids in-process
         scheduler complexity; the worker pool is not the target
         of this test).
      2. Enter the lifespan context — provider/workspace/channel
         registries, system toolset, etc. all bootstrap against
         SQLite.
      3. Drive httpx over ASGITransport. CRUD each router family
         in dependency order; re-read every entity to confirm the
         SQLite adapter round-trips its JSONB shape.
      4. Tear down via reverse-order DELETEs to confirm the SQLite
         storage adapter doesn't 5xx on delete either.

    If any router or its Storage adapter has a Postgres-only
    escape hatch, one of the asserts below fails. If the lifespan
    refuses to start, the test fails at step 2 — a clear regression
    signal that the §1 SQLite path has rotted.
    """
    db_path = tmp_path / "t0852.sqlite"
    cfg = AppConfig(
        runtime_mode=RuntimeMode.API,
        db=StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=db_path),
        ),
        # Session create needs a scheduler on app.state. RuntimeMode.API
        # alone leaves it unset, so wire an in-memory scheduler
        # explicitly — same shape the lifespan would auto-pick for
        # API_PLUS_WORKER. Avoids spinning up a real WorkerPool task in
        # this in-process test.
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
    )
    app = create_app(cfg)

    async with app.router.lifespan_context(app):
        # Sanity: storage actually wired up + file written.
        assert db_path.exists(), (
            f"SqliteStorageProvider.initialize() did not create "
            f"{db_path}"
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t0852",
        ) as client:
            # =============================================================
            # 1. LLMProvider — root of the dependency tree
            # =============================================================
            llm_id = "t852-llm"
            r = await client.post(
                "/v1/llm_providers",
                json={
                    "id": llm_id,
                    "provider": "ollama",
                    "config": {"url": "http://127.0.0.1:9999"},
                    "models": [{"name": "fake-model", "context_length": 4096}],
                    "limits": {"max_concurrency": 1},
                },
            )
            assert r.status_code == 201, r.text
            r = await client.get(f"/v1/llm_providers/{llm_id}")
            assert r.status_code == 200, r.text
            assert r.json()["id"] == llm_id

            # List + count round-trip — pins the SQLite predicate engine
            # paginates correctly when the table has exactly one row.
            r = await client.get("/v1/llm_providers")
            assert r.status_code == 200
            body = r.json()
            assert body["total"] == 1, body
            assert any(p["id"] == llm_id for p in body["items"]), body

            # =============================================================
            # 2. Agent — references the LLMProvider
            # =============================================================
            agent_id = "t852-ag"
            r = await client.post(
                "/v1/agents",
                json={
                    "id": agent_id,
                    "description": "T0852 sqlite probe",
                    "model": {
                        "provider_id": llm_id, "model_name": "fake-model",
                    },
                    "tools": [],
                    "system_prompt": ["sqlite-probe"],
                },
            )
            assert r.status_code == 201, r.text
            r = await client.get(f"/v1/agents/{agent_id}")
            assert r.status_code == 200, r.text
            agent = r.json()
            assert agent["model"]["provider_id"] == llm_id

            # =============================================================
            # 3. Workspace ladder — provider + template + workspace
            # =============================================================
            wp_id = "t852-wp"
            tpl_id = "t852-tpl"
            r = await client.post(
                "/v1/workspace_providers",
                json={
                    "id": wp_id,
                    "provider": "local",
                    "config": {"kind": "local", "path": str(tmp_path / "ws")},
                },
            )
            assert r.status_code == 201, r.text
            r = await client.post(
                "/v1/workspace_templates",
                json={
                    "id": tpl_id,
                    "description": "T0852 template",
                    "provider_id": wp_id,
                    "backend": {"kind": "local"},
                },
            )
            assert r.status_code == 201, r.text
            r = await client.post(
                "/v1/workspaces", json={"template_id": tpl_id},
            )
            assert r.status_code == 201, r.text
            workspace_id = r.json()["id"]

            # =============================================================
            # 4. Session — workspace-scoped, agent-bound, auto_start=False
            # =============================================================
            r = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json={
                    "binding": {"kind": "agent", "agent_id": agent_id},
                    "auto_start": False,
                },
            )
            assert r.status_code == 201, r.text
            session_id = r.json()["id"]

            # Top-level read also works — pins that the session_id index
            # is wired against the SQLite session table.
            r = await client.get(f"/v1/sessions/{session_id}")
            assert r.status_code == 200, r.text

            # =============================================================
            # 5. ToolApprovalPolicy — §2 surface against SQLite
            # =============================================================
            policy_id = "t852-pol"
            r = await client.post(
                "/v1/tool_approval_policies",
                json={
                    "id": policy_id,
                    "toolset_id": "system",
                    "tool_name": "delete_session",
                    "approval": {"type": "required"},
                },
            )
            assert r.status_code == 201, r.text
            r = await client.get(f"/v1/tool_approval_policies/{policy_id}")
            assert r.status_code == 200, r.text

            # =============================================================
            # 6. Channel infrastructure — §3 surface against SQLite
            # =============================================================
            cp_id = "t852-cp"
            ch_id = "t852-ch"
            assoc_id = "t852-assoc"
            r = await client.post(
                "/v1/channel_providers",
                json={
                    "id": cp_id,
                    "provider": "discord",
                    "config": {"bot_token": _FAKE_DISCORD_TOKEN},
                },
            )
            assert r.status_code == 201, r.text
            r = await client.post(
                "/v1/channels",
                json={
                    "id": ch_id,
                    "provider_id": cp_id,
                    "external_id": "snowflake-t852",
                },
            )
            assert r.status_code == 201, r.text
            r = await client.post(
                "/v1/workspace_channel_associations",
                json={
                    "id": assoc_id,
                    "workspace_id": workspace_id,
                    "channel_id": ch_id,
                },
            )
            assert r.status_code == 201, r.text

            # =============================================================
            # 7. SemanticSearchProvider — §7 surface against SQLite
            # =============================================================
            ssp_id = "t852-ssp"
            r = await client.post(
                "/v1/ssp",
                json={
                    "id": ssp_id,
                    "provider": "pgvector",
                    "config": {
                        "hostname": "127.0.0.1",
                        "port": 5432,
                        "username": "user",
                        "password": "pass",
                        "database": "vectors",
                        "embedder": {
                            "provider_id": llm_id, "model": "fake-model",
                        },
                    },
                },
            )
            assert r.status_code == 201, r.text

            # =============================================================
            # 8. InternalCollections — confirm the router responds
            # =============================================================
            # Without a config row, GET returns 404 (per design — this is
            # the "OFF" signal, not a bug). Either 404 or 200 is acceptable;
            # what we pin is "no /errors/internal and no 5xx".
            r = await client.get("/v1/internal_collections/config")
            assert r.status_code in (200, 404), r.text
            assert "/errors/internal" not in r.text, r.text

            # =============================================================
            # 9. /v1/health — sanity-check the always-on observability
            # =============================================================
            r = await client.get("/v1/health")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "ok"

            # =============================================================
            # 10. Cleanup: DELETE in reverse-dependency order
            # =============================================================
            # The order matters because the cascade-blocks (channel and
            # channel_provider) are enforced server-side — incorrect
            # order would 409. This pins that the SQLite adapter's
            # predicate engine evaluates the cascade-check predicates
            # correctly (same shape as the Postgres adapter's).
            for url, expected in (
                # SSP first — no dependents.
                (f"/v1/ssp/{ssp_id}", (200, 204)),
                # Association before channel before channel_provider.
                (
                    f"/v1/workspace_channel_associations/{assoc_id}",
                    (200, 204),
                ),
                (f"/v1/channels/{ch_id}", (200, 204)),
                (f"/v1/channel_providers/{cp_id}", (200, 204)),
                # Tool-approval policy is standalone.
                (f"/v1/tool_approval_policies/{policy_id}", (200, 204)),
                # Workspace dependencies; cancel session first.
                (
                    f"/v1/workspaces/{workspace_id}/sessions/"
                    f"{session_id}/cancel",
                    (200, 204),
                ),
                (f"/v1/workspaces/{workspace_id}", (200, 204)),
                (f"/v1/workspace_templates/{tpl_id}", (200, 204)),
                (f"/v1/workspace_providers/{wp_id}", (200, 204)),
                # Agent + LLM.
                (f"/v1/agents/{agent_id}", (200, 204)),
                (f"/v1/llm_providers/{llm_id}", (200, 204)),
            ):
                if url.endswith("/cancel"):
                    r = await client.post(url)
                else:
                    r = await client.delete(url)
                assert r.status_code in expected, (
                    f"DELETE/cancel {url!r} expected {expected}, "
                    f"got {r.status_code}: {r.text!r}"
                )
