"""The 1 MiB document body cap holds at every user door (KN-12, ruling D5).

User doors: REST ``POST``/``PATCH /docs`` (already capped, 422), REST ``PUT /documents?path=`` (tests/api), the collections
``create_document`` / ``update_document`` tools, the system ``put_document`` tool and the zip import. Internal writers (the system
collection's regeneration, the harness) write through the tree and document services directly and stay uncapped.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from primer.knowledge.document_service import DocumentService
from primer.knowledge.importer import import_zip
from primer.knowledge.tree import DocumentTreeService
from primer.model.collection import Collection
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests._support.caller import AsCaller

CAP = 1024 * 1024
AT_CAP = "x" * CAP
OVER_CAP = "é" * (CAP // 2) + "x"  # CAP // 2 + 1 characters, CAP + 1 UTF-8 bytes: the cap counts bytes


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    await provider.get_storage(Collection).create(Collection(id="kb", description="kb"))
    yield provider
    await provider.aclose()


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, text in entries.items():
            zf.writestr(name, text.encode("utf-8"))
    return buf.getvalue()


async def _body(sp, path: str) -> str | None:
    try:
        return (await DocumentTreeService(sp).read(collection_id="kb", path=path)).body
    except Exception:  # noqa: BLE001 - absent
        return None


def _collections(sp):
    from primer.toolset.collections import build_collections_toolset

    return AsCaller(build_collections_toolset(storage_provider=sp))


def _system(sp):
    from unittest.mock import AsyncMock

    from primer.toolset.system import build_system_toolset

    return AsCaller(build_system_toolset(storage_provider=sp, provider_registry=AsyncMock(), semantic_search_registry=AsyncMock()))


# ---- characterization: at the cap every door accepts; internal writers are uncapped --------------------------------------------------


async def test_internal_writers_are_not_capped(sp):
    tree = DocumentTreeService(sp)
    await tree.create(collection_id="kb", parent="", slug="big", body=OVER_CAP)
    await tree.update(collection_id="kb", path="big", body=OVER_CAP + "y")
    await DocumentService(sp).upsert(collection_id="kb", path="bigger", content=OVER_CAP)
    assert await _body(sp, "big") == OVER_CAP + "y"
    assert await _body(sp, "bigger") == OVER_CAP


async def test_import_accepts_an_entry_at_the_cap(sp):
    report = await import_zip(DocumentTreeService(sp), collection_id="kb", data=_zip({"full.md": AT_CAP}))
    assert report.created == ["full"] and report.rejected == []


async def test_collections_tools_accept_a_body_at_the_cap(sp):
    tools = _collections(sp)
    made = await tools.call(tool_name="create_document", arguments={"collection": "kb", "parent": "", "slug": "a", "body": AT_CAP})
    assert not made.is_error, made.output
    changed = await tools.call(tool_name="update_document", arguments={"collection": "kb", "path": "a", "body": AT_CAP[:-1] + "y"})
    assert not changed.is_error, changed.output
    assert await _body(sp, "a") == AT_CAP[:-1] + "y"


async def test_put_document_accepts_a_body_at_the_cap(sp):
    res = await _system(sp).call(tool_name="put_document", arguments={"collection_id": "kb", "path": "a", "content": AT_CAP})
    assert not res.is_error, res.output
    assert await _body(sp, "a") == AT_CAP


# ---- red: one byte over is refused at every user door ----------------------------------------------------------------------------------


async def test_import_rejects_an_entry_over_the_cap(sp):
    report = await import_zip(DocumentTreeService(sp), collection_id="kb", data=_zip({"big.md": OVER_CAP, "ok.md": "fine"}))
    assert report.created == ["ok"]
    assert report.rejected == [{"file": "big.md", "reason": "document body exceeds the 1 MiB cap"}]
    assert await _body(sp, "big") is None


async def test_collections_create_document_refuses_a_body_over_the_cap(sp):
    res = await _collections(sp).call(
        tool_name="create_document", arguments={"collection": "kb", "parent": "", "slug": "a", "body": OVER_CAP},
    )
    assert res.is_error and json.loads(res.output)["type"] == "validation-error", res.output[:300]
    assert "1 MiB" in json.loads(res.output)["message"]
    assert await _body(sp, "a") is None


async def test_collections_update_document_refuses_a_body_over_the_cap(sp):
    tools = _collections(sp)
    await tools.call(tool_name="create_document", arguments={"collection": "kb", "parent": "", "slug": "a", "body": "v1"})
    res = await tools.call(tool_name="update_document", arguments={"collection": "kb", "path": "a", "body": OVER_CAP})
    assert res.is_error and json.loads(res.output)["type"] == "validation-error", res.output[:300]
    assert await _body(sp, "a") == "v1"


async def test_put_document_refuses_a_body_over_the_cap(sp):
    res = await _system(sp).call(tool_name="put_document", arguments={"collection_id": "kb", "path": "a", "content": OVER_CAP})
    assert res.is_error and json.loads(res.output)["type"] == "validation-error", res.output[:300]
    assert "1 MiB" in json.loads(res.output)["message"]
    assert await _body(sp, "a") is None
