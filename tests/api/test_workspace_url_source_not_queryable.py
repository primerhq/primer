"""A url file source's password cannot be searched, sorted or sought through a cursor by a role=user caller (ticket 01a11d32, the B1 of the #721 review).

A template's ``files`` and a workspace's ``overrides`` hold ``kind=url`` sources whose password the read serves masked (``MaskedUserinfoUrl``) while the stored row keeps it. A predicate, an
``order_by`` or a cursor's seek key is compared with the STORED document, so before #725 classified a field that holds a masked type anywhere in its tree, ``POST /v1/workspace_templates/find``
with ``files ILIKE '%s3cr3t%'`` was a prefix oracle and ``?order_by=files`` a sort oracle on the password. Against the REAL app over SQLite, as role=user (the template routes are user-tier):

* a find predicate on ``files`` / ``overrides`` answers 422 and names no password;
* ``?order_by=files`` / ``?order_by=overrides`` answer 422;
* a forged cursor naming ``files`` answers 400 and serves no row;
* a plain field (``provider_id``) still finds and sorts.
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
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.model.workspace import FileMount, Workspace, WorkspaceRuntimeMeta, WorkspaceTemplateOverrides, _UrlSource
from primer.storage.sqlite import SqliteStorageProvider

_TEMPLATE_PASSWORD = "s3cr3t"
_OVERRIDE_PASSWORD = "ovpass99"


def _forge_cursor(field: str, value) -> str:
    keys = [
        {"field": field, "value": value, "direction": "asc", "is_null": value is None},
        {"field": "id", "value": "", "direction": "asc", "is_null": False},
    ]
    payload = json.dumps({"keys": keys}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def _like(field: str, pattern: str) -> dict:
    return {
        "predicate": {"left": {"kind": "field", "name": field}, "op": "~=*", "right": {"kind": "value", "value": pattern}},
        "page": {"kind": "offset", "offset": 0, "length": 50},
    }


@pytest_asyncio.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[dict]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "urlq.sqlite"))
    await sp.initialize()
    reg = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=reg)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin, httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as user:
        r = await admin.post("/v1/auth/register", json={"username": "adm", "password": "admpassword1"})
        assert r.status_code == 200, r.text
        await sp.get_storage(User).create(
            User(id="user-u", username="uu", password_hash=await hash_password("uupassword1"), created_at=datetime.now(timezone.utc), role="user")
        )
        r = await user.post("/v1/auth/login", json={"username": "uu", "password": "uupassword1"})
        assert r.status_code == 200, r.text
        created = await admin.post(
            "/v1/workspace_templates",
            json={
                "id": "tpl-a", "provider_id": "p-loc", "description": "d",
                "files": [{"path": "seed.txt", "source": {"kind": "url", "url": f"https://reader:{_TEMPLATE_PASSWORD}@files.example.com/seed.txt"}}],
            },
        )
        assert created.status_code in (200, 201), created.text
        await sp.get_storage(Workspace).create(
            Workspace(
                id="ws-one",
                template_id="tpl-a",
                provider_id="p-loc",
                created_at=datetime.now(timezone.utc),
                overrides=WorkspaceTemplateOverrides(
                    files=[FileMount(path="extra.txt", source=_UrlSource(url=f"https://reader:{_OVERRIDE_PASSWORD}@files.example.com/extra.txt"))]
                ),
                runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:1/", token=SecretStr("t")),
            )
        )
        yield {"admin": admin, "user": user, "sp": sp}
    await sp.aclose()


@pytest.mark.asyncio
async def test_the_user_reads_the_template_and_the_workspace_masked(ctx) -> None:
    """The premise: what the reader is allowed to see is the masked form; the queries below must not give them more."""
    tpl = await ctx["user"].get("/v1/workspace_templates/tpl-a")
    ws = await ctx["user"].get("/v1/workspaces/ws-one")
    assert tpl.status_code == 200 and ws.status_code == 200, (tpl.text, ws.text)
    assert _TEMPLATE_PASSWORD not in tpl.text and _OVERRIDE_PASSWORD not in ws.text
    assert "**********" in tpl.text and "**********" in ws.text


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", [f"%{_TEMPLATE_PASSWORD[:3]}%", "%reader:s3%", "%files.example.com%"], ids=["a prefix of the password", "user and password", "a host (the whole field is JSON text)"])
async def test_a_user_find_on_template_files_is_refused(ctx, pattern: str) -> None:
    r = await ctx["user"].post("/v1/workspace_templates/find", json=_like("files", pattern))
    assert r.status_code == 422, r.text
    assert r.json()["type"] == "/errors/validation-error"
    assert _TEMPLATE_PASSWORD not in r.text and "tpl-a" not in r.text


@pytest.mark.asyncio
async def test_a_user_find_inside_template_files_by_path_is_refused_too(ctx) -> None:
    body = {
        "predicate": {"left": {"kind": "field", "name": "files.0.source.url"}, "op": "~=*", "right": {"kind": "value", "value": "%s3c%"}},
        "page": {"kind": "offset", "offset": 0, "length": 50},
    }
    r = await ctx["user"].post("/v1/workspace_templates/find", json=body)
    assert r.status_code == 422, r.text
    assert _TEMPLATE_PASSWORD not in r.text


@pytest.mark.asyncio
async def test_a_user_order_by_template_files_is_refused(ctx) -> None:
    for direction in ("asc", "desc"):
        r = await ctx["user"].get("/v1/workspace_templates", params={"order_by": f"files:{direction}"})
        assert r.status_code == 422, r.text
        assert r.json()["type"] == "/errors/validation-error"
        assert _TEMPLATE_PASSWORD not in r.text


@pytest.mark.asyncio
async def test_a_forged_cursor_naming_template_files_is_refused_and_serves_no_row(ctx) -> None:
    for guess in ("a", "z"):
        r = await ctx["user"].get("/v1/workspace_templates", params={"cursor": _forge_cursor("files", guess)})
        assert r.status_code == 400, r.text
        assert r.headers["content-type"].startswith("application/problem+json")
        assert "tpl-a" not in r.text and _TEMPLATE_PASSWORD not in r.text


@pytest.mark.asyncio
async def test_a_user_find_on_workspace_overrides_holding_a_url_password_is_refused(ctx) -> None:
    r = await ctx["user"].post("/v1/workspaces/find", json=_like("overrides", f"%{_OVERRIDE_PASSWORD[:4]}%"))
    assert r.status_code == 422, r.text
    assert r.json()["type"] == "/errors/validation-error"
    assert _OVERRIDE_PASSWORD not in r.text and "ws-one" not in r.text


@pytest.mark.asyncio
async def test_a_user_order_by_workspace_overrides_is_refused(ctx) -> None:
    r = await ctx["user"].get("/v1/workspaces", params={"order_by": "overrides:asc"})
    assert r.status_code == 422, r.text
    assert r.json()["type"] == "/errors/validation-error"
    assert _OVERRIDE_PASSWORD not in r.text


@pytest.mark.asyncio
async def test_a_plain_field_still_finds_and_sorts(ctx) -> None:
    listed = await ctx["user"].get("/v1/workspace_templates", params={"order_by": "provider_id:asc"})
    assert listed.status_code == 200, listed.text
    assert [t["id"] for t in listed.json()["items"]] == ["tpl-a"]
    body = {
        "predicate": {"left": {"kind": "field", "name": "provider_id"}, "op": "=", "right": {"kind": "value", "value": "p-loc"}},
        "page": {"kind": "offset", "offset": 0, "length": 50},
    }
    found = await ctx["user"].post("/v1/workspace_templates/find", json=body)
    assert found.status_code == 200, found.text
    assert [t["id"] for t in found.json()["items"]] == ["tpl-a"]
    assert _TEMPLATE_PASSWORD not in found.text
