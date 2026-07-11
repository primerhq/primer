"""Tests for the harness REST router (Task 10).

Covers the 7 scenarios specified in the plan:
1. POST creates a DRAFT row; token round-trips redacted via GET.
2. POST rejects duplicate slug with 409.
3. POST/{id}/fetch flips pending_operation=fetch; second call returns 409.
4. PUT/{id}/overrides validates against cached schema; 422 on invalid; 200 + recomputed hash on valid.
5. POST/{id}/install rejects with 422 when no schema cached.
6. POST/{id}/install rejects with 409 if pending_operation already set.
7. DELETE flips pending_operation=uninstall and returns 202.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from primer.harness.hashes import hash_overrides
from primer.model.harness import (
    Harness,
    HarnessDirection,
    HarnessOperation,
    HarnessStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_harness(**kwargs) -> Harness:
    defaults = dict(
        id="hns_test000001",
        slug="my-test-harness",
        name="My Test Harness",
        git_url="https://github.com/example/repo",
        status=HarnessStatus.DRAFT,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return Harness(**defaults)


# ---------------------------------------------------------------------------
# Scenario 1: POST creates DRAFT row; token redacted in GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_harness_returns_draft(client):
    resp = await client.post(
        "/v1/harnesses",
        json={
            "name": "My Harness",
            "slug": "my-harness",
            "git_url": "https://github.com/example/repo",
            "git_token": "super-secret-token",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "draft"
    assert body["slug"] == "my-harness"
    assert body["name"] == "My Harness"
    assert body["id"].startswith("hns_")
    # Token must be redacted
    assert body["git_token"] == "**********"

    harness_id = body["id"]

    # GET also returns redacted token
    get_resp = await client.get(f"/v1/harnesses/{harness_id}")
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["git_token"] == "**********"


# ---------------------------------------------------------------------------
# Scenario 2: POST rejects duplicate slug with 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_harness_duplicate_slug(client):
    payload = {
        "name": "First",
        "slug": "unique-slug",
        "git_url": "https://github.com/example/repo",
    }
    r1 = await client.post("/v1/harnesses", json=payload)
    assert r1.status_code == 201

    r2 = await client.post("/v1/harnesses", json={"name": "Second", "slug": "unique-slug", "git_url": "https://github.com/example/repo2"})
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# Scenario 3: POST/{id}/fetch flips pending_operation; second call 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_flips_pending_operation_and_conflicts_on_second(client, app):
    # Create harness
    r = await client.post(
        "/v1/harnesses",
        json={"name": "H", "slug": "fetch-test", "git_url": "https://github.com/x/y"},
    )
    assert r.status_code == 201
    hid = r.json()["id"]

    # First fetch: 202
    r2 = await client.post(f"/v1/harnesses/{hid}/fetch")
    assert r2.status_code == 202
    assert r2.json()["pending_operation"] == "fetch"

    # Second fetch: 409 (pending_op already set)
    r3 = await client.post(f"/v1/harnesses/{hid}/fetch")
    assert r3.status_code == 409


# ---------------------------------------------------------------------------
# Scenario 4: PUT/{id}/overrides validates against schema; 422 invalid; 200 valid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_overrides_validates_schema(client, app, fake_storage_provider):
    # Pre-insert a harness with an overrides_schema directly into storage
    schema = {
        "type": "object",
        "properties": {"model": {"type": "string"}},
        "required": ["model"],
        "additionalProperties": False,
    }
    harness = _make_harness(
        id="hns_overridetest",
        slug="override-test",
        overrides_schema=schema,
        status=HarnessStatus.READY,
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    # Invalid overrides (missing required "model")
    bad_resp = await client.put(
        "/v1/harnesses/hns_overridetest/overrides",
        json={"bad_field": "value"},
    )
    assert bad_resp.status_code == 422
    bad_body = bad_resp.json()
    assert bad_body.get("code") == "overrides_invalid" or (
        "code" in str(bad_body) or "overrides_invalid" in str(bad_body)
    )

    # Valid overrides
    good_resp = await client.put(
        "/v1/harnesses/hns_overridetest/overrides",
        json={"model": "gpt-4"},
    )
    assert good_resp.status_code == 200
    good_body = good_resp.json()
    assert good_body["overrides"] == {"model": "gpt-4"}
    # overrides_hash should be populated and match
    expected_hash = hash_overrides({"model": "gpt-4"})
    assert good_body["overrides_hash"] == expected_hash


# ---------------------------------------------------------------------------
# Scenario 5: POST/{id}/install rejects with 422 when no schema cached
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_rejects_no_schema(client, app, fake_storage_provider):
    # Harness with READY status but no overrides_schema
    harness = _make_harness(
        id="hns_noschema",
        slug="no-schema",
        status=HarnessStatus.READY,
        overrides_schema=None,
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_noschema/install")
    assert r.status_code == 422
    body = r.json()
    assert "overrides_schema_missing" in str(body)


# ---------------------------------------------------------------------------
# Scenario 6: POST/{id}/install rejects with 409 if pending_operation set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_rejects_pending_op(client, app, fake_storage_provider):
    # Harness with READY status, a schema, and pending_operation already set
    schema = {"type": "object", "properties": {}, "additionalProperties": True}
    harness = _make_harness(
        id="hns_pendingop",
        slug="pending-op",
        status=HarnessStatus.READY,
        overrides_schema=schema,
        pending_operation=HarnessOperation.FETCH,
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_pendingop/install")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Scenario 7: DELETE flips pending_operation=uninstall; 202
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_enqueues_uninstall(client, app, fake_storage_provider):
    """DELETE enqueues UNINSTALL (202). An inbound harness with no ?cascade
    defaults to cascade — uninstall removes the installed objects."""
    harness = _make_harness(
        id="hns_todelete",
        slug="to-delete",
        status=HarnessStatus.INSTALLED,
        direction=HarnessDirection.INBOUND,
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.delete("/v1/harnesses/hns_todelete")
    assert r.status_code == 202
    body = r.json()
    assert body["pending_operation"] == "uninstall"
    assert body["uninstall_cascade"] is True  # inbound default: cascade
    assert (await storage.get("hns_todelete")).uninstall_cascade is True


@pytest.mark.asyncio
async def test_delete_outbound_default_keeps_entities(client, app, fake_storage_provider):
    """An OUTBOUND harness deleted with no ?cascade defaults to non-cascade —
    only the harness is removed; the objects it tracks are kept."""
    harness = _make_harness(
        id="hns_outdel",
        slug="out-del",
        status=HarnessStatus.INSTALLED,
        direction=HarnessDirection.OUTBOUND,
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.delete("/v1/harnesses/hns_outdel")
    assert r.status_code == 202
    assert r.json()["uninstall_cascade"] is False
    assert (await storage.get("hns_outdel")).uninstall_cascade is False


@pytest.mark.asyncio
async def test_delete_explicit_cascade_overrides_direction(client, app, fake_storage_provider):
    """An explicit ?cascade= overrides the direction default either way."""
    storage = fake_storage_provider.get_storage(Harness)
    # Outbound + ?cascade=true -> cascade despite the safe default.
    await storage.create(_make_harness(
        id="hns_ovr1", slug="ovr1", status=HarnessStatus.INSTALLED,
        direction=HarnessDirection.OUTBOUND,
    ))
    r = await client.delete("/v1/harnesses/hns_ovr1?cascade=true")
    assert r.status_code == 202
    assert (await storage.get("hns_ovr1")).uninstall_cascade is True
    # Inbound + ?cascade=false -> keep the installed objects.
    await storage.create(_make_harness(
        id="hns_ovr2", slug="ovr2", status=HarnessStatus.INSTALLED,
        direction=HarnessDirection.INBOUND,
    ))
    r = await client.delete("/v1/harnesses/hns_ovr2?cascade=false")
    assert r.status_code == 202
    assert (await storage.get("hns_ovr2")).uninstall_cascade is False


# ---------------------------------------------------------------------------
# Extra: GET list returns empty list initially
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_harnesses_empty(client):
    r = await client.get("/v1/harnesses")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert body["items"] == []


# ---------------------------------------------------------------------------
# Extra: GET 404 on missing harness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_harness_not_found(client):
    r = await client.get("/v1/harnesses/hns_doesnotexist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Extra: PUT updates name/description; marks overrides_dirty on ref change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_harness_marks_overrides_dirty_on_ref_change(client, app, fake_storage_provider):
    harness = _make_harness(
        id="hns_refdirty",
        slug="ref-dirty",
        ref="main",
        status=HarnessStatus.READY,
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    # Change ref → should mark overrides_dirty=True
    r = await client.put("/v1/harnesses/hns_refdirty", json={"ref": "v2"})
    assert r.status_code == 200
    body = r.json()
    assert body["overrides_dirty"] is True
    assert body["ref"] == "v2"


# ---------------------------------------------------------------------------
# Extra: overrides_schema_missing returns well-formed code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_overrides_no_schema_returns_422(client, app, fake_storage_provider):
    harness = _make_harness(
        id="hns_noschema2",
        slug="no-schema-2",
        status=HarnessStatus.READY,
        overrides_schema=None,
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.put("/v1/harnesses/hns_noschema2/overrides", json={"x": 1})
    assert r.status_code == 422
    body = r.json()
    assert "overrides_schema_missing" in str(body)


# ---------------------------------------------------------------------------
# Extra: sync rejects when status not INSTALLED/OUTDATED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_rejects_wrong_status(client, app, fake_storage_provider):
    harness = _make_harness(
        id="hns_syncwrong",
        slug="sync-wrong",
        status=HarnessStatus.DRAFT,
        available_bundle_hash="abc123",
    )
    storage = fake_storage_provider.get_storage(Harness)
    await storage.create(harness)

    r = await client.post("/v1/harnesses/hns_syncwrong/sync")
    assert r.status_code == 409


# ===========================================================================
# ClaimEngine integration
# ===========================================================================


class _FakeClaimEngine:
    """Minimal spy for upsert / delete_lease calls."""

    def __init__(self) -> None:
        self.upserted: list[tuple] = []
        self.deleted: list[tuple] = []

    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None):
        self.upserted.append((kind, entity_id, {"priority": priority}))

    async def delete_lease(self, kind, entity_id):
        self.deleted.append((kind, entity_id))


@pytest.fixture
def app_with_engine(fake_storage_provider, fake_provider_registry):
    from primer.api.app import create_test_app
    _app = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )
    _engine = _FakeClaimEngine()
    _app.state.claim_engine = _engine
    return _app, _engine


@pytest.mark.asyncio
async def test_claim_engine_upsert_on_fetch(app_with_engine, fake_storage_provider):
    """POST /{id}/fetch calls engine.upsert(HARNESS, hid, priority=10)."""
    from primer.int.claim import ClaimKind

    _app, engine = app_with_engine
    harness = _make_harness(id="hns_fetch_eng", slug="fetch-eng")
    await fake_storage_provider.get_storage(Harness).create(harness)

    async with AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://t",
    ) as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:
            pass
        r = await c.post("/v1/harnesses/hns_fetch_eng/fetch")
        assert r.status_code == 202

    matched = [u for u in engine.upserted if u[0] == ClaimKind.HARNESS and u[1] == "hns_fetch_eng"]
    assert matched, f"Expected engine.upsert(HARNESS, 'hns_fetch_eng') but got: {engine.upserted!r}"
    assert matched[0][2]["priority"] == 10


@pytest.mark.asyncio
async def test_claim_engine_upsert_on_delete_uninstall(app_with_engine, fake_storage_provider):
    """DELETE /{id} (enqueues UNINSTALL) calls engine.upsert(HARNESS, hid, priority=10)."""
    from primer.int.claim import ClaimKind

    _app, engine = app_with_engine
    harness = _make_harness(id="hns_del_eng", slug="del-eng", status=HarnessStatus.INSTALLED)
    await fake_storage_provider.get_storage(Harness).create(harness)

    async with AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://t",
    ) as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:
            pass
        r = await c.delete("/v1/harnesses/hns_del_eng")
        assert r.status_code == 202

    matched = [u for u in engine.upserted if u[0] == ClaimKind.HARNESS and u[1] == "hns_del_eng"]
    assert matched, f"Expected engine.upsert(HARNESS, 'hns_del_eng') but got: {engine.upserted!r}"
    assert matched[0][2]["priority"] == 10


@pytest.mark.asyncio
async def test_claim_engine_upsert_on_sync(app_with_engine, fake_storage_provider):
    """POST /{id}/sync calls engine.upsert(HARNESS, hid, priority=10)."""
    from primer.int.claim import ClaimKind

    _app, engine = app_with_engine
    harness = _make_harness(
        id="hns_sync_eng",
        slug="sync-eng",
        status=HarnessStatus.INSTALLED,
        available_bundle_hash="abc",
    )
    await fake_storage_provider.get_storage(Harness).create(harness)

    async with AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://t",
    ) as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:
            pass
        r = await c.post("/v1/harnesses/hns_sync_eng/sync")
        assert r.status_code == 202

    matched = [u for u in engine.upserted if u[0] == ClaimKind.HARNESS and u[1] == "hns_sync_eng"]
    assert matched, f"Expected engine.upsert(HARNESS, 'hns_sync_eng') but got: {engine.upserted!r}"
    assert matched[0][2]["priority"] == 10


@pytest.mark.asyncio
async def test_claim_engine_upsert_on_install(app_with_engine, fake_storage_provider):
    """POST /{id}/install calls engine.upsert(HARNESS, hid, priority=10)."""
    from primer.int.claim import ClaimKind

    _app, engine = app_with_engine
    schema = {"type": "object", "properties": {}, "additionalProperties": True}
    harness = _make_harness(
        id="hns_inst_eng",
        slug="inst-eng",
        status=HarnessStatus.READY,
        overrides_schema=schema,
    )
    await fake_storage_provider.get_storage(Harness).create(harness)

    async with AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://t",
    ) as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:
            pass
        r = await c.post("/v1/harnesses/hns_inst_eng/install")
        assert r.status_code == 202

    matched = [u for u in engine.upserted if u[0] == ClaimKind.HARNESS and u[1] == "hns_inst_eng"]
    assert matched, f"Expected engine.upsert(HARNESS, 'hns_inst_eng') but got: {engine.upserted!r}"
    assert matched[0][2]["priority"] == 10


@pytest.mark.asyncio
async def test_claim_engine_none_is_noop_for_harness(fake_storage_provider, fake_provider_registry):
    """When claim_engine is absent from app.state, harness ops still work."""
    from primer.api.app import create_test_app

    _app = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )
    # no engine set

    harness = _make_harness(id="hns_noop", slug="noop-hns")
    await fake_storage_provider.get_storage(Harness).create(harness)

    async with AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://t",
    ) as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:
            pass
        r = await c.post("/v1/harnesses/hns_noop/fetch")
        assert r.status_code == 202
