from primer.workspace.files import FileResolvers


def test_file_resolvers_defaults_none():
    fr = FileResolvers()
    assert fr.document_resolver is None
    assert fr.secret_resolver is None


def test_file_resolvers_holds_callables():
    async def doc(_fm):
        return b"d"

    async def sec(_fm):
        return b"s"

    fr = FileResolvers(document_resolver=doc, secret_resolver=sec)
    assert fr.document_resolver is doc
    assert fr.secret_resolver is sec


import pytest
from pydantic import SecretStr

from primer.model.collection import Document
from primer.model.workspace import FileMount
from primer.workspace.resolvers import (
    make_document_resolver,
    make_secret_resolver,
)


class _StubStorage:
    def __init__(self, doc):
        self._doc = doc

    async def get(self, id, *, conn=None):
        if self._doc is not None and self._doc.id == id:
            return self._doc
        return None


class _StubStorageProvider:
    def __init__(self, doc):
        self._storage = _StubStorage(doc)

    def get_storage(self, model_class):
        return self._storage


class _StubSecretProvider:
    def __init__(self, secrets):
        self._secrets = secrets

    async def get_secret(self, name):
        val = self._secrets.get(name)
        return SecretStr(val) if val is not None else None


def _doc_mount(collection_id="col1", document_id="document-abc"):
    return FileMount(
        path="seed/doc.txt",
        source={
            "kind": "document",
            "collection_id": collection_id,
            "document_id": document_id,
        },
    )


def _secret_mount(name="deploy_key"):
    return FileMount(
        path="seed/key.txt",
        source={"kind": "secret", "name": name},
    )


@pytest.mark.asyncio
async def test_document_resolver_happy_text_key():
    doc = Document(id="document-abc", collection_id="col1", name="d", path="d.md", meta={"text": "hello body"})
    resolver = make_document_resolver(_StubStorageProvider(doc))
    assert await resolver(_doc_mount()) == b"hello body"


@pytest.mark.asyncio
async def test_document_resolver_happy_content_key():
    doc = Document(id="document-abc", collection_id="col1", name="d", path="d.md", meta={"content": "alt body"})
    resolver = make_document_resolver(_StubStorageProvider(doc))
    assert await resolver(_doc_mount()) == b"alt body"


@pytest.mark.asyncio
async def test_document_resolver_missing_document_raises():
    resolver = make_document_resolver(_StubStorageProvider(None))
    with pytest.raises(RuntimeError, match="document-abc"):
        await resolver(_doc_mount())


@pytest.mark.asyncio
async def test_document_resolver_collection_mismatch_raises():
    doc = Document(id="document-abc", collection_id="OTHER", name="d", path="d.md", meta={"text": "x"})
    resolver = make_document_resolver(_StubStorageProvider(doc))
    with pytest.raises(RuntimeError, match="collection"):
        await resolver(_doc_mount(collection_id="col1"))


@pytest.mark.asyncio
async def test_document_resolver_empty_body_raises():
    doc = Document(id="document-abc", collection_id="col1", name="d", path="d.md", meta={})
    resolver = make_document_resolver(_StubStorageProvider(doc))
    with pytest.raises(RuntimeError, match="empty"):
        await resolver(_doc_mount())


@pytest.mark.asyncio
async def test_secret_resolver_hit():
    resolver = make_secret_resolver(_StubSecretProvider({"deploy_key": "s3cr3t"}))
    assert await resolver(_secret_mount()) == b"s3cr3t"


@pytest.mark.asyncio
async def test_secret_resolver_miss_raises():
    resolver = make_secret_resolver(_StubSecretProvider({}))
    with pytest.raises(RuntimeError, match="deploy_key"):
        await resolver(_secret_mount())
