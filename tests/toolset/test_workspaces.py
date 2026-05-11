"""Direct unit tests for the ``_workspaces`` toolset.

The end-to-end happy-path through HTTP is covered by
``tests/api/test_workspaces.py``; this file targets the toolset's
catalog assembly + the bootstrap-tool-ingestion path that ensures
the workspaces tools land in the ``_internal_tools`` collection when
the internal collections subsystem activates.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from matrix.api.registries import WorkspaceRegistry
from matrix.internal_collections import (
    INTERNAL_COLLECTION_IDS,
    build_subsystem,
)
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.internal import (
    INTERNAL_COLLECTIONS_CONFIG_ID,
    InternalCollectionsConfig,
)
from matrix.model.storage import OffsetPage, OffsetPageResponse
from matrix.model.workspace import (
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
)
from matrix.toolset.workspaces import (
    WORKSPACES_TOOLSET_ID,
    build_workspaces_toolset,
)


# ===========================================================================
# In-memory fakes
# ===========================================================================


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id):
        return self._data.get(id)

    async def create(self, e):
        if e.id in self._data:
            raise ConflictError(f"id {e.id!r} already exists")
        self._data[e.id] = e
        return e

    async def update(self, e):
        if e.id not in self._data:
            raise NotFoundError(f"no entity with id {e.id!r}")
        self._data[e.id] = e
        return e

    async def delete(self, id):
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            return OffsetPageResponse(
                offset=page.offset,
                length=len(items[page.offset : page.offset + page.length]),
                total=len(items),
                items=items[page.offset : page.offset + page.length],
            )
        return OffsetPageResponse(
            offset=0, length=len(items), total=len(items), items=items
        )

    async def find(self, predicate, page, *, order_by=None):
        return await self.list(page, order_by=order_by)


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls):
        return self._stores.setdefault(cls, _Storage())


class _LiveSession:
    """Minimal session stub for tool-handler tests."""

    def __init__(self, session_id="sess-1") -> None:
        from datetime import datetime, timezone
        from matrix.model.session import SessionStatus

        self.session_id = session_id
        self._status = SessionStatus.RUNNING

    async def info(self):
        from datetime import datetime, timezone
        from matrix.model.session import SessionInfo

        return SessionInfo(
            session_id=self.session_id,
            agent_id="agt",
            workspace_id="ws-stub",
            parent_session_id=None,
            status=self._status,
            started_at=datetime.now(timezone.utc),
            last_activity_at=datetime.now(timezone.utc),
        )

    async def status(self):
        return self._status

    async def request_pause(self):
        from matrix.model.session import SessionStatus

        self._status = SessionStatus.PAUSED

    async def request_resume(self):
        from matrix.model.session import SessionStatus

        self._status = SessionStatus.RUNNING

    async def append_instruction(self, content):
        from datetime import datetime, timezone
        from matrix.model.session import Instruction

        return Instruction(
            instruction_id="i-1",
            session_id=self.session_id,
            content=content,
            queued_at=datetime.now(timezone.utc),
        )


class _LiveWorkspace:
    """In-memory live workspace satisfying the ABC for handler tests."""

    def __init__(self, workspace_id="ws-stub") -> None:
        self.id = workspace_id
        self._files: dict[str, bytes] = {}
        self._sessions: dict[str, _LiveSession] = {"sess-1": _LiveSession()}

    async def list_files(self, path=".", *, recursive=False):
        from datetime import datetime, timezone
        from matrix.model.workspace import FileEntry

        return [
            FileEntry(
                path=p,
                kind="file",
                size_bytes=len(c),
                modified_at=datetime.now(timezone.utc),
            )
            for p, c in self._files.items()
        ]

    async def file_info(self, path):
        from datetime import datetime, timezone
        from matrix.model.workspace import FileEntry

        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return FileEntry(
            path=path,
            kind="file",
            size_bytes=len(self._files[path]),
            modified_at=datetime.now(timezone.utc),
        )

    async def read_file(self, path):
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    async def write_file(self, path, content):
        self._files[path] = content

    async def delete_file(self, path):
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        del self._files[path]

    async def log(self, *, limit=50):
        from datetime import datetime, timezone
        from matrix.workspace.local.state import CommitInfo

        return [
            CommitInfo(
                sha="a" * 40,
                subject="init",
                committed_at=datetime.now(timezone.utc),
                workspace_id=self.id,
            )
        ]

    async def list_sessions(self, *, agent_id=None, status=None):
        out = []
        for s in self._sessions.values():
            out.append(await s.info())
        return out

    async def get_session(self, session_id):
        return self._sessions.get(session_id)

    async def aclose(self):
        return


class _StubBackend:
    def __init__(self, _provider) -> None:
        self._workspaces: dict[str, Any] = {}

    async def initialize(self):
        return

    async def aclose(self):
        return

    async def create(self, template, *, overrides=None):
        ws = _LiveWorkspace("ws-stub")
        self._workspaces[ws.id] = ws
        return ws

    async def get(self, workspace_id):
        return self._workspaces.get(workspace_id)

    async def list(self):
        return list(self._workspaces.keys())

    async def destroy(self, workspace_id):
        if workspace_id not in self._workspaces:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        del self._workspaces[workspace_id]


# Minimal vector store + embedder for the bootstrap path.
class _FakeVectorStore:
    def __init__(self) -> None:
        self.collections: dict[str, dict] = {}
        self.records: dict[tuple[str, str, str], Any] = {}

    async def create_collection(
        self, collection_id, *, dimensions, distance="cosine"
    ):
        self.collections[collection_id] = {
            "dimensions": dimensions,
            "distance": distance,
        }

    async def put(self, record):
        self.records[
            (record.collection_id, record.document_id, record.chunk_id)
        ] = record

    async def delete(self, cid, doc_id):
        for key in list(self.records.keys()):
            if key[0] == cid and key[1] == doc_id:
                del self.records[key]

    async def search(self, cid, vector, k):
        return []


class _FakeVSR:
    def __init__(self, store) -> None:
        self._store = store
        self.is_configured = True

    async def get(self):
        return self._store

    async def aclose(self):
        return


class _FakeEmbedder:
    async def embed(self, *, model, inputs, **kwargs):
        class _R:
            embeddings = [type("E", (), {"vector": [0.1, 0.2, 0.3, 0.4]})()]

        return _R()


class _FakePR:
    def __init__(self, embedder) -> None:
        self._embedder = embedder

    async def get_embedder(self, _provider_id):
        return self._embedder

    async def get_toolset(self, toolset_id):
        raise NotFoundError(f"unknown toolset {toolset_id!r}")


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def workspace_registry(sp) -> WorkspaceRegistry:
    return WorkspaceRegistry(sp, factory=_StubBackend)


@pytest.fixture
def toolset(sp, workspace_registry):
    return build_workspaces_toolset(
        storage_provider=sp,
        workspace_registry=workspace_registry,
    )


def _provider() -> WorkspaceProvider:
    return WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(path="/tmp/x"),
    )


def _template_body() -> dict:
    return {
        "id": "tpl-1",
        "description": "dev",
        "provider_id": "local-1",
    }


@pytest.fixture
async def seeded(toolset):
    """Seed provider + template + workspace via the toolset itself.

    Returns the workspace id for tests that exercise sub-resources.
    """
    await toolset.call(
        tool_name="create_workspace_provider",
        arguments={"entity": _provider().model_dump(mode="json")},
    )
    await toolset.call(
        tool_name="create_workspace_template",
        arguments={"entity": _template_body()},
    )
    create = await toolset.call(
        tool_name="create_workspace",
        arguments={"id": "ws-stub", "template_id": "tpl-1"},
    )
    assert not create.is_error, create.output
    return "ws-stub"


# ===========================================================================
# Catalog
# ===========================================================================


class TestCatalog:
    @pytest.mark.asyncio
    async def test_toolset_id_and_count(self, toolset) -> None:
        assert WORKSPACES_TOOLSET_ID == "_workspaces"
        names = [t.id async for t in toolset.list_tools()]
        assert len(names) == 24

    @pytest.mark.asyncio
    async def test_every_tool_has_clear_description(self, toolset) -> None:
        async for tool in toolset.list_tools():
            assert tool.toolset_id == WORKSPACES_TOOLSET_ID
            assert len(tool.description) > 30


# ===========================================================================
# Provider CRUD via toolset
# ===========================================================================


class TestProviderCRUD:
    @pytest.mark.asyncio
    async def test_create_get_delete(self, toolset) -> None:
        body = _provider().model_dump(mode="json")

        result = await toolset.call(
            tool_name="create_workspace_provider",
            arguments={"entity": body},
        )
        assert not result.is_error, result.output

        result = await toolset.call(
            tool_name="get_workspace_provider", arguments={"id": "local-1"}
        )
        assert not result.is_error
        assert json.loads(result.output)["id"] == "local-1"

        result = await toolset.call(
            tool_name="delete_workspace_provider", arguments={"id": "local-1"}
        )
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_get_unknown_returns_not_found(self, toolset) -> None:
        result = await toolset.call(
            tool_name="get_workspace_provider", arguments={"id": "missing"}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"


# ===========================================================================
# Bootstrap-tool-ingestion: the workspaces toolset's tools end up in
# the _internal_tools collection.
# ===========================================================================


class TestBootstrapIngestsWorkspacesTools:
    @pytest.mark.asyncio
    async def test_subsystem_bootstrap_ingests_workspace_tools(
        self, sp, workspace_registry, toolset
    ) -> None:
        store = _FakeVectorStore()
        vsr = _FakeVSR(store)
        embedder = _FakeEmbedder()
        pr = _FakePR(embedder)

        cfg = InternalCollectionsConfig(
            id=INTERNAL_COLLECTIONS_CONFIG_ID,
            embedding_provider_id="hf-1",
            embedding_model="all-MiniLM-L6-v2",
            cross_encoder=None,
            mmr=None,
            activated_at=None,
        )

        subsystem = build_subsystem(
            config=cfg,
            storage_provider=sp,
            provider_registry=pr,
            vector_store_registry=vsr,
            toolset_providers={"_workspaces": toolset},
        )

        result = await subsystem.bootstrap()
        assert result["ok"] is True

        tools_coll = INTERNAL_COLLECTION_IDS["tool"]
        ingested_ids = {
            doc_id
            for (cid, doc_id, _) in store.records.keys()
            if cid == tools_coll
        }
        ws_ingested = {
            doc_id
            for doc_id in ingested_ids
            if doc_id.startswith("_workspaces::")
        }
        assert len(ws_ingested) == 24
        for expected in (
            "_workspaces::list_workspace_providers",
            "_workspaces::create_workspace_template",
            "_workspaces::list_workspace_files",
            "_workspaces::pause_workspace_session",
            "_workspaces::get_workspace_log",
        ):
            assert expected in ws_ingested, f"missing {expected}"

        await subsystem.aclose()


# ===========================================================================
# Sub-resource handlers exercised through the toolset against an in-memory
# live workspace (covers list_files / write / read / delete / log / sessions).
# ===========================================================================


class TestSubResourceHandlers:
    @pytest.mark.asyncio
    async def test_list_workspaces_via_toolset(self, toolset, seeded) -> None:
        result = await toolset.call(tool_name="list_workspaces", arguments={})
        assert not result.is_error
        page = json.loads(result.output)
        assert page["total"] >= 1

    @pytest.mark.asyncio
    async def test_get_workspace(self, toolset, seeded) -> None:
        result = await toolset.call(
            tool_name="get_workspace", arguments={"id": seeded}
        )
        assert not result.is_error
        assert json.loads(result.output)["id"] == seeded

    @pytest.mark.asyncio
    async def test_template_full_crud(self, toolset) -> None:
        body = _template_body()
        await toolset.call(
            tool_name="create_workspace_template", arguments={"entity": body}
        )
        body["description"] = "v2"
        upd = await toolset.call(
            tool_name="update_workspace_template",
            arguments={"id": "tpl-1", "entity": body},
        )
        assert not upd.is_error
        assert json.loads(upd.output)["description"] == "v2"
        get = await toolset.call(
            tool_name="get_workspace_template", arguments={"id": "tpl-1"}
        )
        assert json.loads(get.output)["description"] == "v2"
        delete = await toolset.call(
            tool_name="delete_workspace_template", arguments={"id": "tpl-1"}
        )
        assert not delete.is_error

    @pytest.mark.asyncio
    async def test_template_update_id_mismatch(self, toolset) -> None:
        body = _template_body()
        await toolset.call(
            tool_name="create_workspace_template", arguments={"entity": body}
        )
        upd = await toolset.call(
            tool_name="update_workspace_template",
            arguments={"id": "different", "entity": body},
        )
        assert upd.is_error
        assert json.loads(upd.output)["type"] == "conflict"

    @pytest.mark.asyncio
    async def test_session_ops(self, toolset, seeded) -> None:
        listed = await toolset.call(
            tool_name="list_workspace_sessions",
            arguments={"workspace_id": seeded},
        )
        assert not listed.is_error
        assert json.loads(listed.output)["total"] >= 1

        got = await toolset.call(
            tool_name="get_workspace_session",
            arguments={"workspace_id": seeded, "session_id": "sess-1"},
        )
        assert not got.is_error

        pause = await toolset.call(
            tool_name="pause_workspace_session",
            arguments={"workspace_id": seeded, "session_id": "sess-1"},
        )
        assert not pause.is_error
        resume = await toolset.call(
            tool_name="resume_workspace_session",
            arguments={"workspace_id": seeded, "session_id": "sess-1"},
        )
        assert not resume.is_error
        steer = await toolset.call(
            tool_name="steer_workspace_session",
            arguments={
                "workspace_id": seeded,
                "session_id": "sess-1",
                "instruction": "go",
            },
        )
        assert not steer.is_error
        assert json.loads(steer.output)["content"] == "go"

    @pytest.mark.asyncio
    async def test_file_ops_text_round_trip(self, toolset, seeded) -> None:
        write = await toolset.call(
            tool_name="write_workspace_file",
            arguments={
                "workspace_id": seeded,
                "path": "hi.txt",
                "content": "hello",
                "encoding": "text",
            },
        )
        assert not write.is_error
        info = await toolset.call(
            tool_name="get_workspace_file_info",
            arguments={"workspace_id": seeded, "path": "hi.txt"},
        )
        assert not info.is_error
        listed = await toolset.call(
            tool_name="list_workspace_files",
            arguments={"workspace_id": seeded, "path": "."},
        )
        assert not listed.is_error
        read = await toolset.call(
            tool_name="read_workspace_file",
            arguments={"workspace_id": seeded, "path": "hi.txt"},
        )
        assert json.loads(read.output)["content"] == "hello"
        delete = await toolset.call(
            tool_name="delete_workspace_file",
            arguments={"workspace_id": seeded, "path": "hi.txt"},
        )
        assert not delete.is_error

    @pytest.mark.asyncio
    async def test_file_ops_base64_round_trip(self, toolset, seeded) -> None:
        import base64

        payload = b"\x00\x01\x02bin"
        b64 = base64.b64encode(payload).decode()
        await toolset.call(
            tool_name="write_workspace_file",
            arguments={
                "workspace_id": seeded,
                "path": "bin",
                "content": b64,
                "encoding": "base64",
            },
        )
        read = await toolset.call(
            tool_name="read_workspace_file",
            arguments={
                "workspace_id": seeded,
                "path": "bin",
                "encoding": "base64",
            },
        )
        body = json.loads(read.output)
        assert base64.b64decode(body["content"]) == payload

    @pytest.mark.asyncio
    async def test_file_unknown_encoding(self, toolset, seeded) -> None:
        result = await toolset.call(
            tool_name="write_workspace_file",
            arguments={
                "workspace_id": seeded,
                "path": "x",
                "content": "y",
                "encoding": "weird",
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_file_invalid_base64_payload(self, toolset, seeded) -> None:
        result = await toolset.call(
            tool_name="write_workspace_file",
            arguments={
                "workspace_id": seeded,
                "path": "x",
                "content": "not-base64!@#$",
                "encoding": "base64",
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_log(self, toolset, seeded) -> None:
        result = await toolset.call(
            tool_name="get_workspace_log",
            arguments={"workspace_id": seeded, "limit": 10},
        )
        assert not result.is_error
        assert "commits" in json.loads(result.output)

    @pytest.mark.asyncio
    async def test_create_workspace_404_on_missing_template(
        self, toolset
    ) -> None:
        await toolset.call(
            tool_name="create_workspace_provider",
            arguments={"entity": _provider().model_dump(mode="json")},
        )
        result = await toolset.call(
            tool_name="create_workspace",
            arguments={"template_id": "no-such-tpl"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_delete_workspace_via_toolset(
        self, toolset, seeded
    ) -> None:
        result = await toolset.call(
            tool_name="delete_workspace", arguments={"id": seeded}
        )
        assert not result.is_error
        # Subsequent get returns not-found
        get = await toolset.call(
            tool_name="get_workspace", arguments={"id": seeded}
        )
        assert get.is_error


# ===========================================================================
# Error-path coverage for the generic CRUD handlers + sub-resources
# ===========================================================================


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_list_validation_error(self, toolset) -> None:
        result = await toolset.call(
            tool_name="list_workspace_providers",
            arguments={"limit": -1},  # ge=1
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"

    @pytest.mark.asyncio
    async def test_list_offset_and_cursor_together(self, toolset) -> None:
        result = await toolset.call(
            tool_name="list_workspace_providers",
            arguments={"offset": 0, "cursor": "abc"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_list_invalid_order_by(self, toolset) -> None:
        result = await toolset.call(
            tool_name="list_workspace_providers",
            arguments={"order_by": ["id:bogus"]},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_get_validation_error(self, toolset) -> None:
        result = await toolset.call(
            tool_name="get_workspace_provider", arguments={}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"

    @pytest.mark.asyncio
    async def test_create_duplicate_returns_conflict(self, toolset) -> None:
        body = _provider().model_dump(mode="json")
        first = await toolset.call(
            tool_name="create_workspace_provider", arguments={"entity": body}
        )
        assert not first.is_error
        dup = await toolset.call(
            tool_name="create_workspace_provider", arguments={"entity": body}
        )
        assert dup.is_error
        assert json.loads(dup.output)["type"] == "conflict"

    @pytest.mark.asyncio
    async def test_create_validation_error(self, toolset) -> None:
        result = await toolset.call(
            tool_name="create_workspace_provider",
            arguments={"entity": {"id": "x"}},  # missing required fields
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"

    @pytest.mark.asyncio
    async def test_update_validation_error(self, toolset) -> None:
        result = await toolset.call(
            tool_name="update_workspace_template",
            arguments={"id": "x", "entity": {"id": "x"}},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"

    @pytest.mark.asyncio
    async def test_update_unknown_returns_not_found(self, toolset) -> None:
        body = _template_body()
        body["id"] = "missing"
        result = await toolset.call(
            tool_name="update_workspace_template",
            arguments={"id": "missing", "entity": body},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_delete_unknown_returns_not_found(self, toolset) -> None:
        result = await toolset.call(
            tool_name="delete_workspace_provider", arguments={"id": "missing"}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_delete_workspace_unknown_returns_not_found(
        self, toolset
    ) -> None:
        result = await toolset.call(
            tool_name="delete_workspace", arguments={"id": "no-such"}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_session_ops_unknown_workspace(self, toolset) -> None:
        for tool_name in (
            "list_workspace_sessions",
            "get_workspace_session",
            "pause_workspace_session",
            "resume_workspace_session",
        ):
            args: dict = {"workspace_id": "no-such"}
            if tool_name != "list_workspace_sessions":
                args["session_id"] = "sess-1"
            result = await toolset.call(tool_name=tool_name, arguments=args)
            assert result.is_error, tool_name
            assert json.loads(result.output)["type"] == "not-found", tool_name

    @pytest.mark.asyncio
    async def test_session_ops_unknown_session(self, toolset, seeded) -> None:
        for tool_name in (
            "get_workspace_session",
            "pause_workspace_session",
            "resume_workspace_session",
        ):
            result = await toolset.call(
                tool_name=tool_name,
                arguments={"workspace_id": seeded, "session_id": "missing"},
            )
            assert result.is_error
            assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_steer_unknown_session(self, toolset, seeded) -> None:
        result = await toolset.call(
            tool_name="steer_workspace_session",
            arguments={
                "workspace_id": seeded,
                "session_id": "missing",
                "instruction": "x",
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_steer_unknown_workspace(self, toolset) -> None:
        result = await toolset.call(
            tool_name="steer_workspace_session",
            arguments={
                "workspace_id": "no-such",
                "session_id": "sess-1",
                "instruction": "x",
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_file_ops_unknown_workspace(self, toolset) -> None:
        for tool_name, args in (
            ("list_workspace_files", {"workspace_id": "no-such"}),
            (
                "get_workspace_file_info",
                {"workspace_id": "no-such", "path": "x"},
            ),
            ("read_workspace_file", {"workspace_id": "no-such", "path": "x"}),
            ("delete_workspace_file", {"workspace_id": "no-such", "path": "x"}),
            (
                "write_workspace_file",
                {"workspace_id": "no-such", "path": "x", "content": "y"},
            ),
        ):
            result = await toolset.call(tool_name=tool_name, arguments=args)
            assert result.is_error, tool_name
            assert json.loads(result.output)["type"] == "not-found", tool_name

    @pytest.mark.asyncio
    async def test_read_unknown_encoding(self, toolset, seeded) -> None:
        result = await toolset.call(
            tool_name="read_workspace_file",
            arguments={
                "workspace_id": seeded,
                "path": "x",
                "encoding": "bogus",
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_read_invalid_utf8_text_encoding(
        self, toolset, seeded
    ) -> None:
        # Write binary, read as text → should reject
        import base64

        b64 = base64.b64encode(b"\xff\xfe\x00").decode()
        await toolset.call(
            tool_name="write_workspace_file",
            arguments={
                "workspace_id": seeded,
                "path": "binbin",
                "content": b64,
                "encoding": "base64",
            },
        )
        result = await toolset.call(
            tool_name="read_workspace_file",
            arguments={
                "workspace_id": seeded,
                "path": "binbin",
                "encoding": "text",
            },
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_delete_unknown_file(self, toolset, seeded) -> None:
        result = await toolset.call(
            tool_name="delete_workspace_file",
            arguments={"workspace_id": seeded, "path": "no-such"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_log_unknown_workspace(self, toolset) -> None:
        result = await toolset.call(
            tool_name="get_workspace_log",
            arguments={"workspace_id": "no-such", "limit": 5},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_validation_error_on_each_subresource(self, toolset) -> None:
        # Each sub-resource handler validates its arg model before the
        # workspace lookup; supplying empty args triggers the
        # validation-error branch.
        for tool_name in (
            "list_workspace_sessions",
            "get_workspace_session",
            "pause_workspace_session",
            "resume_workspace_session",
            "steer_workspace_session",
            "list_workspace_files",
            "get_workspace_file_info",
            "read_workspace_file",
            "delete_workspace_file",
            "write_workspace_file",
            "get_workspace_log",
            "create_workspace",
        ):
            result = await toolset.call(tool_name=tool_name, arguments={})
            assert result.is_error, tool_name
            assert json.loads(result.output)["type"] == "validation-error", tool_name


class TestProviderInvalidateCascade:
    """Deleting a WorkspaceProvider drops its cached backend."""

    @pytest.mark.asyncio
    async def test_delete_provider_invalidates_backend(
        self, toolset, sp, workspace_registry
    ) -> None:
        # Create provider and prime the backend cache.
        body = _provider().model_dump(mode="json")
        await toolset.call(
            tool_name="create_workspace_provider", arguments={"entity": body}
        )
        backend = await workspace_registry.get_backend("local-1")
        # ``backend`` is now cached; a sentinel attribute proves identity
        backend.tagged = True  # type: ignore[attr-defined]

        result = await toolset.call(
            tool_name="delete_workspace_provider", arguments={"id": "local-1"}
        )
        assert not result.is_error
        # Re-create the provider and re-fetch — should be a fresh
        # backend (sentinel is gone).
        await toolset.call(
            tool_name="create_workspace_provider", arguments={"entity": body}
        )
        backend2 = await workspace_registry.get_backend("local-1")
        assert not getattr(backend2, "tagged", False)
