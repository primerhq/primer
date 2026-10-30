"""GET /v1/tools and GET /v1/tools/catalogue list only tools the caller may read.

Both endpoints page EVERY stored Toolset row and fan out to each row's tools.
Before the access predicate entered the row query, a plain user B saw the
entry (and the tools) of a toolset owned by another user A, including the
tool names of A's private python toolset. The row query now runs
``visible_predicate(person)`` (spec anti-goal 4: filter in storage): B sees
the built-in toolsets plus the rows B can READ, and sees A's toolset only
after A shares read. Admins keep the unfiltered list. Per-tool public flags
stay Phase 4.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from primer.api.registries import ProviderRegistry
from primer.model.providers.toolset import ToolsetProviderType
from tests.api.conftest import _FakeStorageProvider, app, client  # noqa: F401


@pytest.fixture
def fake_provider_registry(
    fake_storage_provider: _FakeStorageProvider,
) -> ProviderRegistry:
    """Override the module default so a python toolset builds a REAL provider
    (the conftest stubs toolset_factory with object(), which would leave both
    catalogues with no tools to leak)."""

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


async def _owner_and_toolset(
    client, app,
) -> tuple[AsyncClient, str, str]:
    # Python toolset source runs on the host, so creating one is admin-only:
    # the owner here is a second admin (ada), distinct from the first-boot
    # admin the ``client`` fixture logs in as.
    a, a_uid = await _second_user(client, app, "ada", role="admin")
    r = await a.post("/v1/toolsets", json=_py_toolset_body("ts-a"))
    assert r.status_code == 201, r.text
    return a, a_uid, "ts-a"


async def _share_read(a, a_uid: str, b_uid: str, row_id: str) -> None:
    r = await a.put(
        f"/v1/toolsets/{row_id}/access",
        json={"owners": [a_uid], "grants": {b_uid: "read"}},
    )
    assert r.status_code == 200, r.text


def _grouped_entry(r, row_id: str) -> dict | None:
    return next((i for i in r.json()["items"] if i["id"] == row_id), None)


def _grouped_tool_ids(entry: dict) -> list[str]:
    return sorted(t["id"] for t in entry["tools"])


# ---------------------------------------------------------------------------
# GET /v1/tools (per-toolset grouped)
# ---------------------------------------------------------------------------


async def test_tools_listing_hides_a_private_toolset_from_b(actors, client, app):
    _, _, b, _ = actors
    a, _, row_id = await _owner_and_toolset(client, app)
    try:
        r = await b.get("/v1/tools")
        assert r.status_code == 200, r.text
        assert _grouped_entry(r, row_id) is None, "B sees A's private toolset in /v1/tools"
        # The built-ins are still listed for a plain user.
        assert _grouped_entry(r, "system") is not None
    finally:
        await a.aclose()


async def test_tools_listing_shows_a_private_toolset_after_a_shares_read(actors, client, app):
    _, _, b, b_uid = actors
    a, a_uid, row_id = await _owner_and_toolset(client, app)
    try:
        await _share_read(a, a_uid, b_uid, row_id)
        r = await b.get("/v1/tools")
        assert r.status_code == 200, r.text
        entry = _grouped_entry(r, row_id)
        assert entry is not None, "B no longer sees A's toolset after the share"
        assert _grouped_tool_ids(entry) == ["greet"]
    finally:
        await a.aclose()


async def test_tools_listing_shows_a_private_toolset_to_an_admin(client, app):
    a, _, row_id = await _owner_and_toolset(client, app)
    try:
        r = await client.get("/v1/tools")
        assert r.status_code == 200, r.text
        entry = _grouped_entry(r, row_id)
        assert entry is not None, "the admin does not see A's toolset"
        assert _grouped_tool_ids(entry) == ["greet"]
    finally:
        await a.aclose()


# ---------------------------------------------------------------------------
# GET /v1/tools/catalogue (flat, graph editor's ToolCall picker)
# ---------------------------------------------------------------------------


async def test_tools_catalogue_hides_a_private_toolset_from_b(actors, client, app):
    _, _, b, _ = actors
    a, _, row_id = await _owner_and_toolset(client, app)
    try:
        r = await b.get("/v1/tools/catalogue")
        assert r.status_code == 200, r.text
        ids = [i["id"] for i in r.json()["items"]]
        assert f"{row_id}__greet" not in ids, "B sees A's private tool's id in the catalogue"
        # A built-in tool is still in the flat list for a plain user.
        assert "system__ask_user" in ids
    finally:
        await a.aclose()


async def test_tools_catalogue_shows_a_private_toolset_after_a_shares_read(actors, client, app):
    _, _, b, b_uid = actors
    a, a_uid, row_id = await _owner_and_toolset(client, app)
    try:
        await _share_read(a, a_uid, b_uid, row_id)
        r = await b.get("/v1/tools/catalogue")
        assert r.status_code == 200, r.text
        ids = [i["id"] for i in r.json()["items"]]
        assert f"{row_id}__greet" in ids, "B no longer sees A's tool after the share"
    finally:
        await a.aclose()


async def test_tools_catalogue_shows_a_private_toolset_to_an_admin(client, app):
    a, _, row_id = await _owner_and_toolset(client, app)
    try:
        r = await client.get("/v1/tools/catalogue")
        assert r.status_code == 200, r.text
        ids = [i["id"] for i in r.json()["items"]]
        assert f"{row_id}__greet" in ids, "the admin does not see A's tool"
    finally:
        await a.aclose()
