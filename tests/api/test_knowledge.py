"""Phase-3 router tests: Collection + Document."""

from __future__ import annotations

import pytest

from primer.model.collection import Collection, CollectionEmbedder, Document


_SSP_BODY = {
    "id": "ssp-test",
    "provider": "pgvector",
    "config": {
        "hostname": "localhost",
        "port": 5432,
        "database": "primer",
        "username": "primer",
        "password": "primer",
        "db_schema": "public",
    },
}


def _collection(**overrides) -> Collection:
    body = dict(
        id="kb-1",
        description="test collection",
        embedder=CollectionEmbedder(provider_id="hf-1", model="all-MiniLM-L6-v2"),
        search_provider_id="ssp-test",
    )
    body.update(overrides)
    return Collection(**body)


def _document(**overrides) -> Document:
    body = dict(
        id="doc-1",
        collection_id="kb-1",
        name="hello.txt",
        meta={},
    )
    body.update(overrides)
    return Document(**body)


class TestCollectionRouter:
    @pytest.mark.asyncio
    async def test_round_trip(self, client) -> None:
        await client.post("/v1/ssp", json=_SSP_BODY)
        body = _collection().model_dump(mode="json")
        post = await client.post("/v1/collections", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/collections/kb-1")
        assert get.status_code == 200

    @pytest.mark.asyncio
    async def test_list_collection_documents_works_when_collection_exists(
        self, client
    ) -> None:
        await client.post("/v1/ssp", json=_SSP_BODY)
        await client.post("/v1/collections", json=_collection().model_dump(mode="json"))
        await client.post(
            "/v1/documents",
            json=_document(id="doc-1", collection_id="kb-1").model_dump(mode="json"),
        )

        resp = await client.get("/v1/collections/kb-1/documents")
        assert resp.status_code == 200
        # The fake _InMemoryStorage.find ignores predicates and returns
        # the full set; the route still passes through Storage.find,
        # so this test verifies the wiring rather than the predicate
        # evaluation (which is the backend's concern).
        assert resp.json()["length"] >= 1

    @pytest.mark.asyncio
    async def test_list_documents_404_when_collection_missing(self, client) -> None:
        resp = await client.get("/v1/collections/missing/documents")
        assert resp.status_code == 404


class TestSystemCollectionGuard:
    """Documents cannot be hand-ingested into system collections."""

    @pytest.mark.asyncio
    async def test_create_into_system_collection_rejected(self, client) -> None:
        await client.post("/v1/ssp", json=_SSP_BODY)
        sys_coll = _collection(id="_internal_test", system=True).model_dump(
            mode="json"
        )
        # System collections are normally created by internal subsystems;
        # create one directly through storage for the test by posting it.
        created = await client.post("/v1/collections", json=sys_coll)
        assert created.status_code == 201, created.text

        resp = await client.post(
            "/v1/documents",
            json=_document(id="doc-x", collection_id="_internal_test").model_dump(
                mode="json"
            ),
        )
        assert resp.status_code == 400, resp.text
        assert "system-managed" in resp.json().get("detail", "")

    @pytest.mark.asyncio
    async def test_create_into_user_collection_allowed(self, client) -> None:
        await client.post("/v1/ssp", json=_SSP_BODY)
        await client.post(
            "/v1/collections",
            json=_collection(id="kb-user").model_dump(mode="json"),
        )
        resp = await client.post(
            "/v1/documents",
            json=_document(id="doc-ok", collection_id="kb-user").model_dump(
                mode="json"
            ),
        )
        assert resp.status_code == 201, resp.text


class TestDocumentRouter:
    @pytest.mark.asyncio
    async def test_round_trip(self, client) -> None:
        body = _document().model_dump(mode="json")
        post = await client.post("/v1/documents", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/documents/doc-1")
        assert get.status_code == 200


class TestConvertUploadedFile:
    """The /documents/_convert_file endpoint short-circuits docling for
    already-text formats (.md / .txt). Regression test for the upload
    failing on markdown source files."""

    @pytest.mark.asyncio
    async def test_markdown_extension_returns_text_verbatim(self, client):
        body = b"# Hello\n\nThis is *markdown*.\n"
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("note.md", body, "text/markdown")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["filename"] == "note.md"
        assert data["bytes_loaded"] == len(body)
        assert data["text"] == body.decode("utf-8")

    @pytest.mark.asyncio
    async def test_plain_text_extension_returns_text_verbatim(self, client):
        body = b"plain text content"
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("note.txt", body, "text/plain")},
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "plain text content"

    @pytest.mark.asyncio
    async def test_markdown_via_content_type_only(self, client):
        """A file without a known extension but tagged
        text/markdown still goes through the passthrough path."""
        body = b"# heading\nbody\n"
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("blob", body, "text/markdown")},
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == body.decode("utf-8")

    @pytest.mark.asyncio
    async def test_empty_file_rejected(self, client):
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("empty.md", b"", "text/markdown")},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_utf8_text_rejected_with_clear_message(self, client):
        # Latin-1 byte outside ASCII / UTF-8.
        body = b"\xff\xfe garbage"
        resp = await client.post(
            "/v1/documents/_convert_file",
            files={"file": ("bad.md", body, "text/markdown")},
        )
        assert resp.status_code == 400
        assert "UTF-8" in resp.json().get("detail", "")
