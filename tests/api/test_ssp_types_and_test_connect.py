"""GET /v1/ssp/_types and the two SSP test-connect routes (platform wave P3).

Both close out platform wave P2 addendum C's recommendations: a form-
metadata route for the register dropdown, and a reachability probe
that does NOT run VectorStoreProvider.initialize()'s schema/extension
DDL (a real test-connect must not mutate the target database).
"""

from __future__ import annotations

import pytest


def _lance_body(entity_id: str, path: str) -> dict:
    return {"id": entity_id, "provider": "lance", "config": {"path": path}}


def _pgvector_body(entity_id: str, **overrides) -> dict:
    body = {
        "id": entity_id,
        "provider": "pgvector",
        "config": {
            "hostname": "db.invalid",
            "port": 5432,
            "username": "u",
            "password": "p",
            "database": "d",
        },
    }
    body["config"].update(overrides)
    return body


# ---------------------------------------------------------------------------
# GET /v1/ssp/_types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_types_covers_all_three_kinds(client) -> None:
    r = await client.get("/v1/ssp/_types")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"pgvector", "pgvectorscale", "lance"}


@pytest.mark.asyncio
async def test_types_none_are_discoverable(client) -> None:
    """A vector store has no live 'list models' analogue (platform wave
    P2 addendum A's discoverable flag doesn't apply here at all)."""
    body = (await client.get("/v1/ssp/_types")).json()
    for meta in body.values():
        assert meta["discoverable"] is False


@pytest.mark.asyncio
async def test_postgres_family_types_share_the_connection_fields(client) -> None:
    body = (await client.get("/v1/ssp/_types")).json()
    for kind in ("pgvector", "pgvectorscale"):
        keys = {f["key"] for f in body[kind]["config_fields"]}
        assert keys == {"hostname", "port", "username", "password", "database"}
        pw = next(f for f in body[kind]["config_fields"] if f["key"] == "password")
        assert pw["type"] == "password"


@pytest.mark.asyncio
async def test_lance_types_has_only_a_path_field(client) -> None:
    body = (await client.get("/v1/ssp/_types")).json()
    keys = {f["key"] for f in body["lance"]["config_fields"]}
    assert keys == {"path"}


@pytest.mark.asyncio
async def test_types_route_is_not_swallowed_by_the_id_route(client) -> None:
    """The mount-order constraint this route depends on: a regression
    here means _types started 404ing (matched as entity_id="_types")
    or, worse, silently returned whatever GET /ssp/{id} returns for a
    literally-named 'ssp-_types' style id."""
    r = await client.get("/v1/ssp/_types")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)
    assert "provider" not in r.json()  # not a single SemanticSearchProvider row


# ---------------------------------------------------------------------------
# POST /v1/ssp/_test (draft) -- lance, real filesystem
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_test_lance_writable_path_is_ok(client, tmp_path) -> None:
    r = await client.post("/v1/ssp/_test", json={
        "provider": "lance", "config": {"path": str(tmp_path / "lance")},
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


@pytest.mark.asyncio
async def test_draft_test_lance_does_not_create_the_directory(
    client, tmp_path,
) -> None:
    """The probe must not mutate anything - confirms it stays read-only
    even on the happy path (a writable-but-nonexistent leaf under an
    existing writable parent)."""
    target = tmp_path / "not-yet-created"
    r = await client.post("/v1/ssp/_test", json={
        "provider": "lance", "config": {"path": str(target)},
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert not target.exists()


@pytest.mark.asyncio
async def test_draft_test_lance_unwritable_parent_is_not_ok(
    client, tmp_path,
) -> None:
    parent = tmp_path / "locked"
    parent.mkdir(mode=0o500)
    try:
        r = await client.post("/v1/ssp/_test", json={
            "provider": "lance",
            "config": {"path": str(parent / "lance")},
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "error" in body
    finally:
        parent.chmod(0o700)


@pytest.mark.asyncio
async def test_draft_test_invalid_config_shape_is_not_ok(client) -> None:
    r = await client.post("/v1/ssp/_test", json={
        "provider": "lance", "config": {"hostname": "not-a-lance-field"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "error" in body


# ---------------------------------------------------------------------------
# POST /v1/ssp/_test (draft) -- pgvector/pgvectorscale, faked asyncpg
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.closed = False

    async def fetchval(self, query: str):
        if self._fail:
            raise ConnectionRefusedError("could not connect")
        return 1

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_draft_test_postgres_family_success(client, monkeypatch) -> None:
    seen: dict = {}

    async def _fake_connect(**kwargs):
        seen.update(kwargs)
        return _FakeConn(fail=False)

    monkeypatch.setattr("asyncpg.connect", _fake_connect)

    r = await client.post(
        "/v1/ssp/_test", json=_pgvector_body("unused-draft-id"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    # The real password reached asyncpg, never a masked placeholder.
    assert seen["password"] == "p"
    assert seen["host"] == "db.invalid"


@pytest.mark.asyncio
async def test_draft_test_postgres_family_failure_reports_ok_false(
    client, monkeypatch,
) -> None:
    async def _fake_connect(**kwargs):
        raise ConnectionRefusedError("could not connect to server")

    monkeypatch.setattr("asyncpg.connect", _fake_connect)

    r = await client.post(
        "/v1/ssp/_test", json=_pgvector_body("unused-draft-id"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "could not connect" in body["error"]


@pytest.mark.asyncio
async def test_draft_test_never_runs_ddl(client, monkeypatch) -> None:
    """The probe must call asyncpg.connect (a bare connection), never
    asyncpg.create_pool (VectorStoreProvider.initialize()'s own path,
    which follows up with CREATE SCHEMA / CREATE EXTENSION)."""
    async def _fake_connect(**kwargs):
        return _FakeConn(fail=False)

    async def _pool_should_not_be_called(**kwargs):
        raise AssertionError("test-connect must not open a pool / run DDL")

    monkeypatch.setattr("asyncpg.connect", _fake_connect)
    monkeypatch.setattr("asyncpg.create_pool", _pool_should_not_be_called)

    r = await client.post(
        "/v1/ssp/_test", json=_pgvector_body("unused-draft-id"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


@pytest.mark.asyncio
async def test_draft_test_pgvectorscale_uses_the_same_probe(
    client, monkeypatch,
) -> None:
    async def _fake_connect(**kwargs):
        return _FakeConn(fail=False)

    monkeypatch.setattr("asyncpg.connect", _fake_connect)

    body = _pgvector_body("unused")
    body["provider"] = "pgvectorscale"
    r = await client.post("/v1/ssp/_test", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# GET /v1/ssp/{id}/_test (saved) -- reads the real stored row server-side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saved_test_unknown_id_is_404(client) -> None:
    r = await client.get("/v1/ssp/does-not-exist/_test")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_saved_test_lance_round_trip(client, tmp_path) -> None:
    pytest.importorskip("lancedb")
    path = str(tmp_path / "lance")
    create = await client.post("/v1/ssp", json=_lance_body("ssp-test-1", path))
    assert create.status_code == 201, create.text
    try:
        r = await client.get("/v1/ssp/ssp-test-1/_test")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}
    finally:
        await client.delete("/v1/ssp/ssp-test-1")


@pytest.mark.asyncio
async def test_saved_test_postgres_uses_the_stored_unredacted_password(
    client, monkeypatch,
) -> None:
    """The console's GET on a saved row redacts the password
    (SecretStr's default json-mode masking) - this route must still
    probe with the REAL one, read straight from storage, never from a
    request body."""
    seen: dict = {}

    async def _fake_connect(**kwargs):
        seen.update(kwargs)
        return _FakeConn(fail=False)

    monkeypatch.setattr("asyncpg.connect", _fake_connect)

    create = await client.post(
        "/v1/ssp", json=_pgvector_body("ssp-test-2", password="s3cr3t-real"),
    )
    assert create.status_code == 201, create.text
    try:
        got = await client.get("/v1/ssp/ssp-test-2")
        assert got.json()["config"]["password"] != "s3cr3t-real"

        r = await client.get("/v1/ssp/ssp-test-2/_test")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}
        assert seen["password"] == "s3cr3t-real"
    finally:
        await client.delete("/v1/ssp/ssp-test-2")
