"""Tests for primer.workspace.files.resolve_file_sources.

The resolver collapses every ``FileSource`` variant on a ``FileMount`` into a
uniform ``ResolvedFile`` so backends can stop silently skipping non-inline
sources.
"""

from __future__ import annotations

import pytest

from primer.model.workspace import (
    FileMount,
    _DocumentSource,
    _InlineSource,
    _SecretSource,
    _UrlSource,
)
from primer.workspace.files import ResolvedFile, resolve_file_sources


async def test_inline_source_resolved_as_is():
    fm = FileMount(
        path="hello.txt",
        source=_InlineSource(content="hello\n"),
    )
    out = await resolve_file_sources([fm])
    assert out == [ResolvedFile(path="hello.txt", content=b"hello\n", mode=None)]


async def test_inline_source_preserves_mode():
    fm = FileMount(
        path="bin/run",
        source=_InlineSource(content="#!/bin/sh\necho hi\n"),
        mode="0755",
    )
    out = await resolve_file_sources([fm])
    assert out[0].mode == "0755"
    assert out[0].content == b"#!/bin/sh\necho hi\n"


async def test_url_source_fetched(monkeypatch):
    class _FakeResp:
        status = 200

        async def read(self):
            return b'{"a":1}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def get(self, url, **_):
            return _FakeResp()

    monkeypatch.setattr(
        "primer.workspace.files._http_session", lambda: _FakeSession()
    )
    fm = FileMount(
        path="external.json",
        source=_UrlSource(url="https://example.com/x.json"),
    )
    out = await resolve_file_sources([fm])
    assert out[0].path == "external.json"
    assert out[0].content == b'{"a":1}'


async def test_url_source_raises_on_error(monkeypatch):
    class _FakeErrResp:
        status = 500

        async def read(self):
            return b""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _FakeErrSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def get(self, url, **_):
            return _FakeErrResp()

    monkeypatch.setattr(
        "primer.workspace.files._http_session", lambda: _FakeErrSession()
    )
    fm = FileMount(
        path="x",
        source=_UrlSource(url="https://example.com/x"),
    )
    with pytest.raises(RuntimeError, match="500"):
        await resolve_file_sources([fm])


async def test_document_source_requires_resolver():
    fm = FileMount(
        path="d.txt",
        source=_DocumentSource(collection_id="c1", document_id="d1"),
    )
    with pytest.raises(RuntimeError, match="document_resolver"):
        await resolve_file_sources([fm])


async def test_document_source_uses_resolver():
    fm = FileMount(
        path="d.txt",
        source=_DocumentSource(collection_id="c1", document_id="d1"),
    )

    async def _resolver(mount: FileMount) -> bytes:
        assert mount.path == "d.txt"
        assert mount.source.collection_id == "c1"
        assert mount.source.document_id == "d1"
        return b"document-bytes"

    out = await resolve_file_sources([fm], document_resolver=_resolver)
    assert out[0].content == b"document-bytes"


async def test_secret_source_requires_resolver():
    fm = FileMount(
        path="s.env",
        source=_SecretSource(name="api-key"),
    )
    with pytest.raises(RuntimeError, match="secret_resolver"):
        await resolve_file_sources([fm])


async def test_secret_source_uses_resolver():
    fm = FileMount(
        path="s.env",
        source=_SecretSource(name="api-key"),
    )

    async def _resolver(mount: FileMount) -> bytes:
        assert mount.source.name == "api-key"
        return b"sekret"

    out = await resolve_file_sources([fm], secret_resolver=_resolver)
    assert out[0].content == b"sekret"


async def test_mixed_sources_preserve_order(monkeypatch):
    class _FakeResp:
        status = 200

        async def read(self):
            return b"remote"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def get(self, url, **_):
            return _FakeResp()

    monkeypatch.setattr(
        "primer.workspace.files._http_session", lambda: _FakeSession()
    )

    async def _doc(mount):
        return b"doc"

    async def _sec(mount):
        return b"sec"

    mounts = [
        FileMount(path="a", source=_InlineSource(content="inline")),
        FileMount(path="b", source=_UrlSource(url="https://example.com/b")),
        FileMount(
            path="c", source=_DocumentSource(collection_id="c1", document_id="d1")
        ),
        FileMount(path="d", source=_SecretSource(name="k")),
    ]
    out = await resolve_file_sources(
        mounts, document_resolver=_doc, secret_resolver=_sec
    )
    assert [r.path for r in out] == ["a", "b", "c", "d"]
    assert [r.content for r in out] == [b"inline", b"remote", b"doc", b"sec"]
