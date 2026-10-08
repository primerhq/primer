"""``GET /metrics`` is for operators: it needs an admin, unless the deployment says it is public (architecture review A-11).

RUN on the live ingress: an anonymous ``GET /metrics`` answered 200. The Primer registry carries workspace, provider, profile, model,
tool and worker identifiers and how busy each is, which is an inventory of the deployment for anyone who can reach the host. The mount
used to sit outside every router and so outside every auth dependency. It is now behind the same authentication as ``/v1`` (the
session cookie, or a bearer API token, which is what a Prometheus ``authorization`` block sends), and requires the ``admin`` role.
``observability.metrics_public: true`` restores anonymous scraping for a deployment whose network already protects the port; with auth
disabled the synthetic admin passes, as it does everywhere else.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from primer.api.app import create_app
from primer.api.config import AppConfig, AuthConfig, ObservabilityConfig
from primer.auth.api_tokens import extract_prefix, hash_token, mint_plaintext
from primer.auth.passwords import hash_password
from primer.model.api_token import ApiToken
from primer.model.scheduler import RuntimeMode
from primer.model.user import User


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides) -> AppConfig:
    monkeypatch.setenv("HOME", str(tmp_path))
    return AppConfig(runtime_mode=RuntimeMode.API, auto_bootstrap=False, **overrides)


@asynccontextmanager
async def _running(config: AppConfig):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True,
        ) as client:
            yield app, client


async def _user(app, name: str, role: str) -> User:
    user = User(
        id=f"user-{name}", username=name, password_hash=await hash_password("pw"),
        created_at=datetime.now(timezone.utc), role=role,
    )
    await app.state.storage_provider.get_storage(User).create(user)
    return user


async def _sign_in(client, name: str) -> None:
    client.cookies.clear()
    resp = await client.post("/v1/auth/login", json={"username": name, "password": "pw"})
    assert resp.status_code == 200, resp.text


async def _token_of(app, user: User, *, revoked: bool = False) -> str:
    plaintext = mint_plaintext()
    await app.state.storage_provider.get_storage(ApiToken).create(ApiToken(
        id=f"at-{user.username}", user_id=user.id, name="scraper", token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext), created_at=datetime.now(timezone.utc),
        revoked_at=datetime.now(timezone.utc) if revoked else None,
    ))
    return plaintext


@pytest.mark.asyncio
async def test_an_anonymous_scrape_is_refused(tmp_path, monkeypatch):
    async with _running(_config(tmp_path, monkeypatch)) as (_, client):
        resp = await client.get("/metrics")

    assert resp.status_code == 401, resp.text[:200]
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "# TYPE" not in resp.text, "the body of a refused scrape must carry no metric"
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")


@pytest.mark.asyncio
async def test_a_signed_in_non_admin_is_forbidden(tmp_path, monkeypatch):
    async with _running(_config(tmp_path, monkeypatch)) as (app, client):
        await _user(app, "alice", "user")
        await _sign_in(client, "alice")

        resp = await client.get("/metrics")

    assert resp.status_code == 403, resp.text[:200]
    assert "# TYPE" not in resp.text


@pytest.mark.asyncio
async def test_a_signed_in_admin_reads_the_metrics(tmp_path, monkeypatch):
    async with _running(_config(tmp_path, monkeypatch)) as (app, client):
        await _user(app, "root", "admin")
        await _sign_in(client, "root")

        resp = await client.get("/metrics")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"] and "# TYPE" in resp.text


@pytest.mark.asyncio
async def test_a_scraper_with_an_admins_api_token_reads_the_metrics(tmp_path, monkeypatch):
    """What a Prometheus ``authorization: {credentials: ...}`` block sends."""
    async with _running(_config(tmp_path, monkeypatch)) as (app, client):
        token = await _token_of(app, await _user(app, "root", "admin"))

        resp = await client.get("/metrics", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200 and "# TYPE" in resp.text


@pytest.mark.asyncio
async def test_a_non_admins_api_token_and_a_bad_or_revoked_one_are_refused(tmp_path, monkeypatch):
    async with _running(_config(tmp_path, monkeypatch)) as (app, client):
        users_token = await _token_of(app, await _user(app, "alice", "user"))
        revoked = await _token_of(app, await _user(app, "bob", "admin"), revoked=True)

        as_user = await client.get("/metrics", headers={"Authorization": f"Bearer {users_token}"})
        as_revoked = await client.get("/metrics", headers={"Authorization": f"Bearer {revoked}"})
        as_garbage = await client.get("/metrics", headers={"Authorization": "Bearer primer_pat_nonexistent"})

    assert (as_user.status_code, as_revoked.status_code, as_garbage.status_code) == (403, 401, 401)


@pytest.mark.asyncio
async def test_a_deployment_can_say_the_endpoint_is_public(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, observability=ObservabilityConfig(metrics_public=True))
    async with _running(cfg) as (_, client):
        resp = await client.get("/metrics")

    assert resp.status_code == 200 and "# TYPE" in resp.text


@pytest.mark.asyncio
async def test_with_auth_disabled_the_endpoint_is_open_like_the_rest_of_the_api(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, auth=AuthConfig(enabled=False))
    async with _running(cfg) as (_, client):
        resp = await client.get("/metrics")

    assert resp.status_code == 200 and "# TYPE" in resp.text


@pytest.mark.asyncio
async def test_disabled_metrics_are_still_a_404_not_a_401(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, observability=ObservabilityConfig(metrics_enabled=False))
    async with _running(cfg) as (_, client):
        resp = await client.get("/metrics")

    assert resp.status_code == 404
