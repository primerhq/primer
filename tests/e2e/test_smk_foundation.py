"""SMK foundation tests (Phase 2): health, auth, tokens, errors, bug capture.

FND-02 (fresh-bootstrap seeding) and FND-08 (restart persistence) need a
bootstrap-on / bringup server; they skip on the hermetic sqlite server.
"""
from __future__ import annotations

import httpx
import pytest

from tests._support.model_profiles import agent_model, seed_llm_provider
from tests._support.restart import restart_server, under_bringup
from tests._support.smk import smk

pytestmark = pytest.mark.asyncio

_USER = {"username": "e2e", "password": "e2e-password-123"}


@smk("SMK-FND-01")
async def test_health_endpoint(client):
    r = await client.get("/v1/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["scheduler"]["alive"] is True
    assert "capacity" in body["worker_pool"]


@smk("SMK-FND-03")
async def test_register_login_logout(client):
    # The operator user already exists (created by authed_client); a repeat
    # register is rejected, and login then protected-then-logout works.
    reg = await client.post("/v1/auth/register", json=_USER)
    assert reg.status_code in (200, 409), reg.text
    login = await client.post("/v1/auth/login", json=_USER)
    assert login.status_code in (200, 204), login.text
    ok = await client.get("/v1/agents")
    assert ok.status_code == 200, ok.text
    out = await client.post("/v1/auth/logout")
    assert out.status_code == 204, out.text
    after = await client.get("/v1/agents")
    assert after.status_code == 401


@smk("SMK-FND-04")
async def test_api_token_mint_and_bearer(authed_client, base_url, unique_suffix):
    mint = await authed_client.post(
        "/v1/auth/tokens", json={"name": f"tok-{unique_suffix}"}
    )
    assert mint.status_code in (200, 201), mint.text
    body = mint.json()
    secret = body.get("token") or body.get("plaintext") or body.get("secret")
    token_id = body.get("id")
    assert secret, body
    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {secret}"},
        timeout=httpx.Timeout(30.0),
    ) as bearer:
        r = await bearer.get("/v1/agents")
        assert r.status_code == 200, r.text
        # revoke, then the bearer call is rejected
        await authed_client.delete(f"/v1/auth/tokens/{token_id}")
        r2 = await bearer.get("/v1/agents")
        assert r2.status_code == 401


@smk("SMK-FND-05")
async def test_rfc7807_error_envelope(authed_client):
    nf = await authed_client.get("/v1/agents/does-not-exist")
    assert nf.status_code == 404
    body = nf.json()
    for key in ("type", "title", "status"):
        assert key in body, body
    invalid = await authed_client.post("/v1/agents", json={"id": "x"})
    assert invalid.status_code == 422


@smk("SMK-FND-06")
async def test_openapi_and_console_served(client):
    spec = await client.get("/v1/openapi.json")
    assert spec.status_code == 200
    assert any(p.startswith("/v1/") for p in spec.json()["paths"])
    console = await client.get("/console/")
    assert console.status_code == 200


@smk("SMK-FND-02", status="partial")
async def test_bootstrap_status_queryable(authed_client):
    # Full first-boot seeding needs auto_bootstrap on; on the hermetic server
    # (auto_bootstrap off) we only confirm the system-state surface responds.
    r = await authed_client.get("/v1/health")
    assert r.status_code == 200


@smk("SMK-FND-08")
async def test_persistence_survives_restart(authed_client, base_url, unique_suffix):
    if not under_bringup():
        pytest.skip("restart persistence requires the bringup-managed server")
    aid = f"persist-{unique_suffix}"
    pid = f"p-persist-{unique_suffix}"
    # The agent must name a STORED profile (the create is refused otherwise, A-09), so a provider that serves one model gives it one.
    seeded = await seed_llm_provider(
        authed_client,
        {
            "id": pid,
            "provider": "openchat",
            "models": [{"name": "m", "context_length": 8192}],
            "config": {"url": "http://127.0.0.1:1/v1", "flavor": "lmstudio"},
            "limits": {"max_concurrency": 1},
        },
    )
    assert seeded.status_code in (200, 201), seeded.text
    created = await authed_client.post(
        "/v1/agents",
        json={"id": aid, "description": "persist", "model": agent_model(pid, "m"), "tools": []},
    )
    assert created.status_code == 201, created.text
    restart_server(base_url)
    got = await authed_client.get(f"/v1/agents/{aid}")
    assert got.status_code == 200
