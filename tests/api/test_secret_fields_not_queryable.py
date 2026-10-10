"""A secret-bearing field cannot be searched, sorted or sought through a cursor over the REST API (ticket 01a1212a).

Against the REAL app over SQLite. A stored row keeps its secrets in clear and serves them masked, so a predicate / order_by / forged cursor on
such a field answers questions about the value the read hides. These behavioural tests pin the refusals as role=user (and role=admin for the
admin-tier provider routes):

* a forged cursor naming ``git_token`` on the user-tier ``GET /v1/harnesses`` list is a seek on the stored token (before the fix the page it
  returns depends on the guessed first character; after it, a 400 and no row);
* ``?order_by=`` and a find predicate on a secret field answer 422;
* normal pagination on a plain field, and the default id-order cursor, still work.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.auth.passwords import hash_password
from primer.model.harness import Harness, HarnessStatus
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.model.workspace import (
    Workspace,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.storage.sqlite import SqliteStorageProvider

_TOKEN = "secret-token-xyz"       # a stored git_token, served masked
_ENV = "envsecretvalue"           # a stored template env secret


def _forge_cursor(field: str, value, direction: str = "asc") -> str:
    keys = [
        {"field": field, "value": value, "direction": direction, "is_null": value is None},
        {"field": "id", "value": "", "direction": "asc", "is_null": False},
    ]
    payload = json.dumps({"keys": keys}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


@pytest_asyncio.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[dict]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "secq.sqlite"))
    await sp.initialize()
    reg = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=reg)
    tr = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=tr, base_url="http://test") as admin, httpx.AsyncClient(
        transport=tr, base_url="http://test"
    ) as user:
        r = await admin.post("/v1/auth/register", json={"username": "adm", "password": "admpassword1"})
        assert r.status_code == 200, r.text
        await sp.get_storage(User).create(
            User(
                id="user-u",
                username="uu",
                password_hash=await hash_password("uupassword1"),
                created_at=datetime.now(timezone.utc),
                role="user",
            )
        )
        r = await user.post("/v1/auth/login", json={"username": "uu", "password": "uupassword1"})
        assert r.status_code == 200, r.text
        # Seed a harness with a stored git_token (writes are admin-only, so go
        # straight to storage), a template with an env secret, and a workspace
        # with an overrides env secret.
        await sp.get_storage(Harness).create(
            Harness(
                id="hns-secq0001",
                slug="secq-harness",
                name="Secq",
                git_url="https://github.com/example/repo",
                git_token=SecretStr(_TOKEN),
                status=HarnessStatus.READY,
                created_at=datetime.now(timezone.utc),
            )
        )
        await admin.post(
            "/v1/workspace_templates",
            json={"id": "tpl-a", "provider_id": "p-loc", "description": "d", "files": [], "env": {"K": _ENV}},
        )
        await sp.get_storage(Workspace).create(
            Workspace(
                id="ws-one",
                template_id="tpl-a",
                provider_id="p-loc",
                created_at=datetime.now(timezone.utc),
                overrides=WorkspaceTemplateOverrides(env={"K": SecretStr("ovsecret")}),
                runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:1/", token=SecretStr("t")),
            )
        )
        yield {"admin": admin, "user": user, "sp": sp}
    await sp.aclose()


# ---- vector 3: forged cursor on a user-tier list (git_token) ----------------


@pytest.mark.asyncio
async def test_forged_cursor_naming_git_token_is_refused(ctx) -> None:
    user = ctx["user"]
    # A guess that sorts before the real token and one that sorts after it.
    before = await user.get("/v1/harnesses", params={"cursor": _forge_cursor("git_token", "a")})
    after = await user.get("/v1/harnesses", params={"cursor": _forge_cursor("git_token", "z")})
    assert before.status_code == 400, before.text
    assert after.status_code == 400, after.text
    for r in (before, after):
        assert r.headers["content-type"].startswith("application/problem+json")
        assert _TOKEN not in r.text
        # No row is served on the refusal.
        assert '"id":"hns-secq0001"' not in r.text


# ---- vector 1: find predicate on a secret field -----------------------------


@pytest.mark.asyncio
async def test_user_find_ilike_on_template_env_is_refused(ctx) -> None:
    user = ctx["user"]
    body = {
        "predicate": {
            "left": {"kind": "field", "name": "env"},
            "op": "~=*",
            "right": {"kind": "value", "value": f"%{_ENV[:4]}%"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 50},
    }
    r = await user.post("/v1/workspace_templates/find", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["type"] == "/errors/validation-error"
    assert _ENV not in r.text


@pytest.mark.asyncio
async def test_user_find_on_workspace_overrides_is_refused(ctx) -> None:
    user = ctx["user"]
    body = {
        "predicate": {
            "left": {"kind": "field", "name": "overrides"},
            "op": "~=*",
            "right": {"kind": "value", "value": "%ovsec%"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 50},
    }
    r = await user.post("/v1/workspaces/find", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["type"] == "/errors/validation-error"


@pytest.mark.asyncio
async def test_admin_find_like_on_provider_api_key_is_refused(ctx) -> None:
    admin = ctx["admin"]
    await admin.post(
        "/v1/llm_providers",
        json={
            "id": "llm-a",
            "provider": "openchat",
            "models": [{"name": "m", "context_length": 8192}],
            "config": {"url": "http://px.lan/v1", "api_key": "sk-live-0123456789", "flavor": "other"},
            "limits": {"max_concurrency": 1},
        },
    )
    body = {
        "predicate": {
            "left": {"kind": "field", "name": "config.api_key"},
            "op": "~=",
            "right": {"kind": "value", "value": "sk-live%"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 50},
    }
    r = await admin.post("/v1/llm_providers/find", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["type"] == "/errors/validation-error"


# ---- vector 2: order_by on a secret field -----------------------------------


@pytest.mark.asyncio
async def test_user_order_by_template_env_is_refused(ctx) -> None:
    r = await ctx["user"].get("/v1/workspace_templates", params={"order_by": "env:asc"})
    assert r.status_code == 422, r.text
    assert r.json()["type"] == "/errors/validation-error"


@pytest.mark.asyncio
async def test_admin_order_by_provider_config_url_is_refused(ctx) -> None:
    r = await ctx["admin"].get("/v1/llm_providers", params={"order_by": "config.url:asc"})
    assert r.status_code == 422, r.text
    assert r.json()["type"] == "/errors/validation-error"


# ---- the happy paths still work ---------------------------------------------


@pytest.mark.asyncio
async def test_plain_list_and_order_still_work(ctx) -> None:
    user = ctx["user"]
    r = await user.get("/v1/harnesses")
    assert r.status_code == 200, r.text
    assert any(h["id"] == "hns-secq0001" for h in r.json()["items"])

    r = await user.get("/v1/workspace_templates", params={"order_by": "provider_id:asc"})
    assert r.status_code == 200, r.text

    body = {
        "predicate": {
            "left": {"kind": "field", "name": "provider_id"},
            "op": "=",
            "right": {"kind": "value", "value": "p-loc"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 50},
    }
    r = await user.post("/v1/workspace_templates/find", json=body)
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()["items"]] == ["tpl-a"]
