"""Access gates on the collection and document REST surface (access-control spec 6.2, 9; plan P3 Task 1, Decision 6).

The house multi-identity pattern (tests/api/test_access_routes.py): a minimal app with the error handlers, a test-only
middleware that turns an ``X-Test-User: <id>:<role>`` header into ``request.state.actor``, a real SQLite provider, and
the knowledge routers under test. A document inherits its collection: read = read/search/grep/tree, full = write/move/
delete/import, owner = delete the collection, share, enable or disable search. Out-of-access answers 404 with the
missing-row text; an insufficient level answers 403 ``access_denied``.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from primer.api.errors import register_error_handlers
from primer.api.routers import knowledge
from primer.model.access import Access
from primer.model.collection import Collection
from primer.model.principal import Principal
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider

ALICE, BOB, CAROL, DAVE, ROOT = "alice", "bob", "carol", "dave", "root"
CID = "kb"
_MISSING = f"Collection {CID!r} does not exist"
_PAGE = {"kind": "offset", "offset": 0, "length": 50}


def _as(uid: str) -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{'admin' if uid == ROOT else 'user'}"}


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("imported/note.md", "# imported")
    return buf.getvalue()


class _Env:
    def __init__(self, client: httpx.AsyncClient, sp: SqliteStorageProvider) -> None:
        self.client = client
        self.sp = sp


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "kb.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    try:
        for uid in (ALICE, BOB, CAROL, DAVE, ROOT):
            await sp.get_storage(User).create(
                User(id=uid, username=uid, created_at=datetime.now(UTC), role="admin" if uid == ROOT else "user")
            )
        # kb: alice owns it, bob reads it, carol has full access, dave has nothing.
        await sp.get_storage(Collection).create(
            Collection(id=CID, description="wiki", access=Access(owners=[ALICE], grants={BOB: "read", CAROL: "full"}))
        )
        app = FastAPI()
        register_error_handlers(app)
        app.state.storage_provider = sp
        app.state.provider_registry = object()  # nothing on these paths resolves a provider (no search configured)
        app.state.semantic_search_registry = object()  # same: search is off on every seeded collection

        @app.middleware("http")
        async def _actor(request: Request, call_next):
            raw = request.headers.get("X-Test-User")
            if raw:
                uid, role = raw.split(":")
                request.state.actor = Principal(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
            else:
                request.state.actor = None
            return await call_next(request)

        app.include_router(knowledge.collection_access_router, prefix="/v1")
        app.include_router(knowledge.collection_router, prefix="/v1")
        app.include_router(knowledge.document_router, prefix="/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            e = _Env(client, sp)
            # Two documents seeded by the owner: "a" (with a child "a/kid") and "b".
            for parent, slug in (("", "a"), ("a", "kid"), ("", "b")):
                r = await client.post(
                    f"/v1/collections/{CID}/docs", headers=_as(ALICE), json={"parent": parent, "slug": slug, "body": f"body {slug}"}
                )
                assert r.status_code == 201, r.text
            yield e
    finally:
        await sp.aclose()


async def _doc_id(env: _Env, path: str = "a") -> str:
    r = await env.client.get(f"/v1/collections/{CID}/docs", headers=_as(ALICE), params={"path": path})
    assert r.status_code == 200, r.text
    return r.json()["document"]["id"]


# Every door, by the level it needs. Each entry: (method, url, request kwargs).
_READ_DOORS: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", f"/v1/collections/{CID}", {}),
    ("GET", f"/v1/collections/{CID}/access", {}),
    ("GET", f"/v1/collections/{CID}/docs", {"params": {"path": "a"}}),
    ("GET", f"/v1/collections/{CID}/docs", {"params": {"parent": ""}}),
    ("GET", f"/v1/collections/{CID}/grep", {"params": {"q": "body"}}),
    ("GET", f"/v1/collections/{CID}/documents", {}),
    ("GET", f"/v1/collections/{CID}/documents", {"params": {"path": "a"}}),
    ("POST", f"/v1/collections/{CID}/search", {"json": {"query": "q"}}),
    ("GET", f"/v1/collections/{CID}/search", {}),
    ("GET", f"/v1/collections/{CID}/indexed_documents", {}),
]
_FULL_DOORS: list[tuple[str, str, dict[str, Any]]] = [
    ("PUT", f"/v1/collections/{CID}", {"json": {"id": CID, "description": "renamed"}}),
    ("POST", f"/v1/collections/{CID}/docs", {"json": {"parent": "", "slug": "new", "body": "n"}}),
    ("PATCH", f"/v1/collections/{CID}/docs", {"params": {"path": "a"}, "json": {"body": "edited"}}),
    ("POST", f"/v1/collections/{CID}/docs/move", {"json": {"path": "b", "new_parent": "a"}}),
    ("DELETE", f"/v1/collections/{CID}/docs", {"params": {"path": "b"}}),
    ("PUT", f"/v1/collections/{CID}/documents", {"params": {"path": "x"}, "json": {"content": "x"}}),
    ("DELETE", f"/v1/collections/{CID}/documents", {"params": {"path": "b"}}),
    ("POST", f"/v1/collections/{CID}/documents/move", {"json": {"from": "b", "to": "c"}}),
    ("POST", f"/v1/collections/{CID}/import", {"files": {"file": ("a.zip", _zip(), "application/zip")}}),
]
_OWNER_DOORS: list[tuple[str, str, dict[str, Any]]] = [
    ("DELETE", f"/v1/collections/{CID}", {}),
    ("PUT", f"/v1/collections/{CID}/access", {"json": {"owners": [ALICE], "public": True}}),
    (
        "PUT",
        f"/v1/collections/{CID}/search",
        {"json": {"embedder": {"provider_id": "emb", "model": "m"}, "vector_store_provider_id": "ssp"}},
    ),
    ("DELETE", f"/v1/collections/{CID}/search", {}),
]
_ALL_DOORS = _READ_DOORS + _FULL_DOORS + _OWNER_DOORS


def _ids(doors: list[tuple[str, str, dict[str, Any]]]) -> list[str]:
    return [f"{m} {u.split(CID, 1)[1] or '/'} {sorted(kw.get('params', {}))}" for m, u, kw in doors]


def _passed_the_gate(r: httpx.Response) -> bool:
    """The route ran past the access check: a success, or the 409 a collection without search answers."""
    return r.status_code < 400 or (r.status_code == 409 and "search" in r.text)


# --------------------------------------------------------------------------------------------------------------------
# A stranger: every door answers like a missing collection
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "url", "kw"), _ALL_DOORS, ids=_ids(_ALL_DOORS))
async def test_a_private_collection_is_invisible_on_every_door(env: _Env, method: str, url: str, kw: dict) -> None:
    r = await env.client.request(method, url, headers=_as(DAVE), **kw)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == _MISSING


async def test_a_stranger_does_not_list_the_collection(env: _Env) -> None:
    r = await env.client.get("/v1/collections", headers=_as(DAVE))
    assert r.status_code == 200 and r.json()["items"] == []
    r = await env.client.post("/v1/collections/find", headers=_as(DAVE), json={"page": _PAGE})
    assert r.status_code == 200 and r.json()["items"] == []


async def test_a_stranger_sees_no_document_through_the_flat_routes(env: _Env) -> None:
    doc_id = await _doc_id(env)
    r = await env.client.get("/v1/documents", headers=_as(DAVE))
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 0
    r = await env.client.post("/v1/documents/find", headers=_as(DAVE), json={"page": _PAGE})
    assert r.status_code == 200 and r.json()["items"] == []
    # A predicate naming the collection still finds nothing: it only narrows the readable set.
    pred = {"page": _PAGE, "predicate": {"left": {"kind": "field", "name": "collection_id"}, "op": "=", "right": {"kind": "value", "value": CID}}}
    r = await env.client.post("/v1/documents/find", headers=_as(DAVE), json=pred)
    assert r.status_code == 200 and r.json()["items"] == []
    for method in ("GET", "DELETE"):
        r = await env.client.request(method, f"/v1/documents/{doc_id}", headers=_as(DAVE))
        assert r.status_code == 404 and r.json()["detail"] == f"Document {doc_id!r} does not exist", r.text
    body = (await env.client.get(f"/v1/documents/{doc_id}", headers=_as(ALICE))).json()
    r = await env.client.put(f"/v1/documents/{doc_id}", headers=_as(DAVE), json={**body, "title": "t"})
    assert r.status_code == 404 and r.json()["detail"] == f"Document {doc_id!r} does not exist", r.text
    r = await env.client.post(
        "/v1/documents", headers=_as(DAVE), json={"collection_id": CID, "slug": "s", "path": "s"}
    )
    assert r.status_code == 404 and r.json()["detail"] == _MISSING, r.text


# --------------------------------------------------------------------------------------------------------------------
# A read grant: reads pass, every write is 403 access_denied
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "url", "kw"), _READ_DOORS, ids=_ids(_READ_DOORS))
async def test_a_reader_reads(env: _Env, method: str, url: str, kw: dict) -> None:
    r = await env.client.request(method, url, headers=_as(BOB), **kw)
    assert _passed_the_gate(r), r.text


@pytest.mark.parametrize(("method", "url", "kw"), _FULL_DOORS + _OWNER_DOORS, ids=_ids(_FULL_DOORS + _OWNER_DOORS))
async def test_a_reader_cannot_write(env: _Env, method: str, url: str, kw: dict) -> None:
    r = await env.client.request(method, url, headers=_as(BOB), **kw)
    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["code"] == "access_denied"


async def test_a_reader_reads_documents_through_the_flat_routes_but_cannot_write_them(env: _Env) -> None:
    doc_id = await _doc_id(env)
    r = await env.client.get("/v1/documents", headers=_as(BOB))
    assert r.status_code == 200 and r.json()["total"] == 3
    r = await env.client.post("/v1/documents/find", headers=_as(BOB), json={"page": _PAGE})
    assert r.status_code == 200 and len(r.json()["items"]) == 3
    r = await env.client.get(f"/v1/documents/{doc_id}", headers=_as(BOB))
    assert r.status_code == 200, r.text
    body = r.json()
    for method, kw in (("PUT", {"json": {**body, "title": "t"}}), ("DELETE", {})):
        r = await env.client.request(method, f"/v1/documents/{doc_id}", headers=_as(BOB), **kw)
        assert r.status_code == 403 and r.json()["extensions"]["code"] == "access_denied", r.text
    r = await env.client.post("/v1/documents", headers=_as(BOB), json={"collection_id": CID, "slug": "s", "path": "s"})
    assert r.status_code == 403 and r.json()["extensions"]["code"] == "access_denied", r.text


async def test_public_collection_reads_like_a_read_grant(env: _Env) -> None:
    await env.sp.get_storage(Collection).create(Collection(id="pub", description="d", access=Access(owners=[ALICE], public=True)))
    assert (await env.client.get("/v1/collections/pub/documents", headers=_as(DAVE))).status_code == 200
    r = await env.client.put("/v1/collections/pub/documents", headers=_as(DAVE), params={"path": "x"}, json={"content": "x"})
    assert r.status_code == 403 and r.json()["extensions"]["code"] == "access_denied"
    listed = (await env.client.get("/v1/collections", headers=_as(DAVE))).json()["items"]
    assert [c["id"] for c in listed] == ["pub"]


# --------------------------------------------------------------------------------------------------------------------
# A full grant: document writes pass; delete, share and search toggles are owner-only
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "url", "kw"), _READ_DOORS + _FULL_DOORS, ids=_ids(_READ_DOORS + _FULL_DOORS))
async def test_a_full_grantee_reads_and_writes_documents(env: _Env, method: str, url: str, kw: dict) -> None:
    r = await env.client.request(method, url, headers=_as(CAROL), **kw)
    assert _passed_the_gate(r), r.text


@pytest.mark.parametrize(("method", "url", "kw"), _OWNER_DOORS, ids=_ids(_OWNER_DOORS))
async def test_a_full_grantee_cannot_delete_share_or_toggle_search(env: _Env, method: str, url: str, kw: dict) -> None:
    r = await env.client.request(method, url, headers=_as(CAROL), **kw)
    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["code"] == "access_denied"


async def test_a_full_grantee_writes_documents_through_the_flat_routes(env: _Env) -> None:
    doc_id = await _doc_id(env, "b")
    body = (await env.client.get(f"/v1/documents/{doc_id}", headers=_as(CAROL))).json()
    r = await env.client.put(f"/v1/documents/{doc_id}", headers=_as(CAROL), json={**body, "title": "titled"})
    assert r.status_code == 200 and r.json()["title"] == "titled", r.text
    r = await env.client.post("/v1/documents", headers=_as(CAROL), json={"collection_id": CID, "slug": "s", "path": "s"})
    assert r.status_code == 201, r.text
    r = await env.client.delete(f"/v1/documents/{doc_id}", headers=_as(CAROL))
    assert r.status_code == 204, r.text


async def test_moving_a_document_into_an_unwritable_collection_is_refused(env: _Env) -> None:
    await env.sp.get_storage(Collection).create(Collection(id="other", description="d", access=Access(owners=[ALICE], grants={CAROL: "read"})))
    doc_id = await _doc_id(env, "b")
    body = (await env.client.get(f"/v1/documents/{doc_id}", headers=_as(CAROL))).json()
    r = await env.client.put(f"/v1/documents/{doc_id}", headers=_as(CAROL), json={**body, "collection_id": "other"})
    assert r.status_code == 403 and r.json()["extensions"]["code"] == "access_denied", r.text
    r = await env.client.put(f"/v1/documents/{doc_id}", headers=_as(CAROL), json={**body, "collection_id": "nope"})
    assert r.status_code == 404, r.text


# --------------------------------------------------------------------------------------------------------------------
# The owner and an admin
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "url", "kw"), _ALL_DOORS, ids=_ids(_ALL_DOORS))
async def test_the_owner_passes_every_door(env: _Env, method: str, url: str, kw: dict) -> None:
    r = await env.client.request(method, url, headers=_as(ALICE), **kw)
    # PUT /search with no such embedding provider answers 409 after the gate.
    assert _passed_the_gate(r) or (r.status_code == 409 and "embedding provider" in r.text), r.text


@pytest.mark.parametrize(("method", "url", "kw"), _ALL_DOORS, ids=_ids(_ALL_DOORS))
async def test_an_admin_passes_every_door(env: _Env, method: str, url: str, kw: dict) -> None:
    r = await env.client.request(method, url, headers=_as(ROOT), **kw)
    assert _passed_the_gate(r) or (r.status_code == 409 and "embedding provider" in r.text), r.text


async def test_an_admin_sees_every_collection_and_document(env: _Env) -> None:
    await env.sp.get_storage(Collection).create(Collection(id="unowned", description="d"))
    r = await env.client.get("/v1/collections", headers=_as(ROOT))
    assert sorted(c["id"] for c in r.json()["items"]) == [CID, "unowned"]
    r = await env.client.get("/v1/documents", headers=_as(ROOT))
    assert r.json()["total"] == 3


async def test_an_unowned_collection_is_admin_only(env: _Env) -> None:
    """``system`` and ``_internal_*`` have no owners until the Task 3 migration: only admins reach them."""
    await env.sp.get_storage(Collection).create(Collection(id="unowned", description="d"))
    r = await env.client.get("/v1/collections/unowned/documents", headers=_as(ALICE))
    assert r.status_code == 404
    assert (await env.client.get("/v1/collections/unowned/documents", headers=_as(ROOT))).status_code == 200


async def test_the_system_collection_stays_write_protected_for_an_admin(env: _Env) -> None:
    await env.sp.get_storage(Collection).create(Collection(id="sys", description="d", system=True, access=Access(public=True)))
    r = await env.client.put("/v1/collections/sys/documents", headers=_as(ROOT), params={"path": "x"}, json={"content": "x"})
    assert r.status_code == 403 and "system-owned and read-only" in r.text
    r = await env.client.post("/v1/collections/sys/docs", headers=_as(ROOT), json={"parent": "", "slug": "x", "body": ""})
    assert r.status_code == 403 and "system-owned and read-only" in r.text
    # A public system collection reads for everyone and refuses their writes on the level.
    assert (await env.client.get("/v1/collections/sys/documents", headers=_as(DAVE))).status_code == 200
    r = await env.client.put("/v1/collections/sys/documents", headers=_as(DAVE), params={"path": "x"}, json={"content": "x"})
    assert r.status_code == 403


# --------------------------------------------------------------------------------------------------------------------
# Access is never writable through a body
# --------------------------------------------------------------------------------------------------------------------


async def test_a_create_body_cannot_set_access(env: _Env) -> None:
    r = await env.client.post(
        "/v1/collections",
        headers=_as(DAVE),
        json={"id": "mine", "description": "d", "access": {"owners": [ALICE], "grants": {BOB: "full"}, "public": True}},
    )
    assert r.status_code == 201, r.text
    stored = await env.sp.get_storage(Collection).get("mine")
    assert stored.access.owners == [DAVE] and stored.access.grants == {} and stored.access.public is False


async def test_a_whole_row_update_keeps_the_stored_access(env: _Env) -> None:
    before = (await env.sp.get_storage(Collection).get(CID)).access
    # The full grantee tries to make themselves owner and the row public; the owner sends no access at all.
    r = await env.client.put(
        f"/v1/collections/{CID}",
        headers=_as(CAROL),
        json={"id": CID, "description": "x", "access": {"owners": [CAROL], "public": True}},
    )
    assert r.status_code == 200, r.text
    r = await env.client.put(f"/v1/collections/{CID}", headers=_as(ALICE), json={"id": CID, "description": "y"})
    assert r.status_code == 200, r.text
    after = await env.sp.get_storage(Collection).get(CID)
    assert after.description == "y"
    assert after.access.owners == before.owners and after.access.grants == before.grants and after.access.public is False


async def test_the_owner_shares_and_the_grantee_then_reads(env: _Env) -> None:
    r = await env.client.put(f"/v1/collections/{CID}/access", headers=_as(ALICE), json={"owners": [ALICE], "grants": {DAVE: "read"}})
    assert r.status_code == 200, r.text
    assert (await env.client.get(f"/v1/collections/{CID}/docs", headers=_as(DAVE), params={"path": "a"})).status_code == 200
    assert (await env.client.get("/v1/documents", headers=_as(DAVE))).json()["total"] == 3
    # bob lost his read grant with that write.
    assert (await env.client.get(f"/v1/collections/{CID}/grep", headers=_as(BOB), params={"q": "x"})).status_code == 404
