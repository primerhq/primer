"""Ownership wiring for the five owned entities (access-control plan, Phase 1 Task 7).

Two real users (created over the admin user route, each logged in with their
own session cookie) drive the real app: user A owns one row per plural
(a plain user; the toolset row is owned by a second admin because python
toolset creation is admin-only) and user B sees nothing of it in list or get
until A shares read through the access sub-resource. The two routes that
read an owned row outside the CRUD factory (/agents/{id}/status,
/toolsets/{id}/tools) answer the missing-row 404 for B before the share.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from primer.api.registries import ProviderRegistry
from primer.model.model_profile import ModelProfile
from primer.model.providers.toolset import ToolsetProviderType
from tests._support.agent_check_rows import agent_body, profile_row
from tests.api.conftest import _FakeStorageProvider, app, client  # noqa: F401


@pytest.fixture
def fake_provider_registry(
    fake_storage_provider: _FakeStorageProvider,
) -> ProviderRegistry:
    """Override the module default so a python toolset builds a REAL provider
    (the conftest stubs toolset_factory with object(), which would make the
    post-share /tools assertion hit the stub instead of the route)."""

    def _toolset_factory(toolset):
        if toolset.provider == ToolsetProviderType.PYTHON:
            from primer.toolset.python_runner.provider import PythonToolsetProvider
            from primer.toolset.python_runner.runners import LocalHardenedRunner

            return PythonToolsetProvider(
                toolset_id=toolset.id,
                config=toolset.config,
                runner=LocalHardenedRunner(),
            )
        return object()

    return ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=_toolset_factory,
    )


_PW = {
    "alice": "alicepassword",
    "bob": "bobpassword",
    "ada": "adapassword",
}


async def _second_user(
    admin: AsyncClient, app, username: str, role: str = "user"
) -> tuple[AsyncClient, str]:
    """Create a user (and log in a client as it) and return (client, user id)."""
    r = await admin.post(
        "/v1/admin/users",
        json={"username": username, "password": _PW[username], "role": role},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    login = await c.post(
        "/v1/auth/login", json={"username": username, "password": _PW[username]}
    )
    assert login.status_code == 200, login.text
    return c, uid


@pytest.fixture
async def actors(client, app) -> AsyncIterator[tuple[AsyncClient, str, AsyncClient, str]]:
    a, a_uid = await _second_user(client, app, "alice")
    b, b_uid = await _second_user(client, app, "bob")
    try:
        yield a, a_uid, b, b_uid
    finally:
        await a.aclose()
        await b.aclose()


def _ids(r) -> list[str]:
    return sorted(item["id"] for item in r.json()["items"])


async def _share_read(a, a_uid: str, b_uid: str, plural: str, row_id: str) -> None:
    r = await a.put(
        f"/v1/{plural}/{row_id}/access",
        json={"owners": [a_uid], "grants": {b_uid: "read"}},
    )
    assert r.status_code == 200, r.text


async def _hidden_then_shared(
    a, a_uid: str, b, b_uid: str, plural: str, row_id: str
) -> None:
    # Before the share: B sees nothing (the list is filtered, the get
    # answers the missing-row 404).
    r = await b.get(f"/v1/{plural}")
    assert r.status_code == 200, r.text
    assert row_id not in _ids(r)
    r = await b.get(f"/v1/{plural}/{row_id}")
    assert r.status_code == 404, r.text
    # A shares read; B sees the row.
    await _share_read(a, a_uid, b_uid, plural, row_id)
    r = await b.get(f"/v1/{plural}/{row_id}")
    assert r.status_code == 200, r.text
    r = await b.get(f"/v1/{plural}")
    assert row_id in _ids(r)


# ---------------------------------------------------------------------------
# one row per plural: hidden from B until A shares read
# ---------------------------------------------------------------------------


async def test_agent_row_hidden_from_b_until_shared(actors, app):
    a, a_uid, b, b_uid = actors
    await app.state.storage_provider.get_storage(ModelProfile).create(profile_row("mp-1"))
    r = await a.post("/v1/agents", json=agent_body("ag-a"))
    assert r.status_code == 201, r.text
    await _hidden_then_shared(a, a_uid, b, b_uid, "agents", "ag-a")


async def test_graph_row_hidden_from_b_until_shared(actors):
    a, a_uid, b, b_uid = actors
    body = {
        "id": "gr-a",
        "description": "owned wiring probe",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "end", "id": "finish"},
        ],
        "edges": [{"kind": "static", "from_node": "start", "to_node": "finish"}],
    }
    r = await a.post("/v1/graphs", json=body)
    assert r.status_code == 201, r.text
    await _hidden_then_shared(a, a_uid, b, b_uid, "graphs", "gr-a")


async def test_collection_row_and_its_documents_hidden_from_b_until_shared(actors):
    a, a_uid, b, b_uid = actors
    r = await a.post("/v1/collections", json={"id": "kb-a", "description": "owned wiring probe"})
    assert r.status_code == 201, r.text
    r = await a.post("/v1/collections/kb-a/docs", json={"parent": "", "slug": "note", "body": "hello"})
    assert r.status_code == 201, r.text
    r = await b.get("/v1/collections/kb-a/docs", params={"path": "note"})
    assert r.status_code == 404, r.text
    await _hidden_then_shared(a, a_uid, b, b_uid, "collections", "kb-a")
    r = await b.get("/v1/collections/kb-a/docs", params={"path": "note"})
    assert r.status_code == 200 and r.json()["body"] == "hello", r.text


def _py_toolset_body(row_id: str) -> dict:
    return {
        "id": row_id,
        "provider": "python",
        "config": {
            "source": (
                "@primer_tool()\n"
                "def greet(name: str) -> str:\n"
                '    """Greet a person.\n\n    Use when greeting.\n\n'
                "    Args:\n        name: Who.\n    \"\"\"\n"
                "    return 'hi ' + name\n"
            ),
            "source_version": 1,
        },
    }


async def _admin_owner_and_toolset(client, app) -> tuple[AsyncClient, str, str]:
    # Python toolset source runs on the host, so creating one is admin-only
    # (403 forbidden_role): the owner here is a second admin, and
    # admin-owned rows are exactly what the spec describes (admin with a
    # user id owns what they create).
    a, a_uid = await _second_user(client, app, "ada", role="admin")
    r = await a.post("/v1/toolsets", json=_py_toolset_body("ts-a"))
    assert r.status_code == 201, r.text
    return a, a_uid, "ts-a"


async def test_toolset_row_hidden_from_b_until_shared(actors, client, app):
    _, _, b, b_uid = actors
    a, a_uid, row_id = await _admin_owner_and_toolset(client, app)
    try:
        await _hidden_then_shared(a, a_uid, b, b_uid, "toolsets", row_id)
    finally:
        await a.aclose()


async def _create_template(a, client, tmp_path: Path) -> None:
    r = await client.post(
        "/v1/workspace_providers",
        json={
            "id": "p-loc",
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        },
    )
    assert r.status_code == 201, r.text
    r = await a.post(
        "/v1/workspace_templates", json={"id": "tpl-a", "provider_id": "p-loc", "description": "d"}
    )
    assert r.status_code == 201, r.text


async def test_template_row_hidden_from_b_until_shared(actors, client, tmp_path: Path):
    a, a_uid, b, b_uid = actors
    await _create_template(a, client, tmp_path)
    await _hidden_then_shared(a, a_uid, b, b_uid, "workspace_templates", "tpl-a")


async def test_channel_row_hidden_from_b_until_shared(actors, client):
    a, a_uid, b, b_uid = actors
    r = await client.post(
        "/v1/channel_providers",
        json={"id": "cp-discord", "provider": "discord", "config": {"bot_token": "x" * 60}},
    )
    assert r.status_code == 201, r.text
    r = await a.post(
        "/v1/channels",
        json={"id": "ch-a", "provider_id": "cp-discord", "provider": "discord", "external_id": "C1"},
    )
    assert r.status_code == 201, r.text
    await _hidden_then_shared(a, a_uid, b, b_uid, "channels", "ch-a")


# ---------------------------------------------------------------------------
# the routes that read an owned row outside the CRUD factory
# ---------------------------------------------------------------------------


async def test_agent_status_404_for_b_before_sharing(actors, app):
    a, a_uid, b, b_uid = actors
    await app.state.storage_provider.get_storage(ModelProfile).create(profile_row("mp-1"))
    r = await a.post("/v1/agents", json=agent_body("ag-a"))
    assert r.status_code == 201, r.text

    r = await b.get("/v1/agents/ag-a/status")
    assert r.status_code == 404

    await _share_read(a, a_uid, b_uid, "agents", "ag-a")
    r = await b.get("/v1/agents/ag-a/status")
    assert r.status_code == 200, r.text


async def test_toolset_tools_404_for_b_before_sharing(actors, client, app):
    _, _, b, b_uid = actors
    a, a_uid, row_id = await _admin_owner_and_toolset(client, app)
    try:
        r = await b.get(f"/v1/toolsets/{row_id}/tools")
        assert r.status_code == 404

        await _share_read(a, a_uid, b_uid, "toolsets", row_id)
        r = await b.get(f"/v1/toolsets/{row_id}/tools")
        assert r.status_code == 200, r.text
        assert [t["id"] for t in r.json()["tools"]] == ["greet"]
    finally:
        await a.aclose()


async def test_b_cannot_materialise_a_workspace_from_a_private_template(actors, client, tmp_path: Path):
    a, a_uid, b, b_uid = actors
    await _create_template(a, client, tmp_path)

    r = await b.post("/v1/workspaces", json={"id": "ws-b", "template_id": "tpl-a"})
    assert r.status_code == 404, r.text
    # Nothing was materialised.
    assert (await b.get("/v1/workspaces/ws-b")).status_code == 404

    await _share_read(a, a_uid, b_uid, "workspace_templates", "tpl-a")
    r = await b.post("/v1/workspaces", json={"id": "ws-b", "template_id": "tpl-a"})
    assert r.status_code == 201, r.text
