"""End-to-end tests for the workspaces REST surface.

The real :class:`LocalWorkspaceBackend` materialises directories on
the host filesystem; we side-step that here by injecting a custom
:class:`WorkspaceRegistry` whose ``factory`` returns a fully-in-memory
fake backend. That keeps the tests cheap and isolated.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from matrix.api.app import create_test_app
from matrix.api.registries import (
    ProviderRegistry,
    VectorStoreRegistry,
    WorkspaceRegistry,
)
from matrix.model.except_ import (
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from matrix.model.provider import (
    PgVectorConfig,
    VectorStoreProviderConfig,
    VectorStoreProviderType,
)
from matrix.model.session import SessionInfo, SessionStatus
from matrix.model.storage import OffsetPage, OffsetPageResponse
from matrix.model.workspace import (
    FileEntry,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)


# ===========================================================================
# In-memory storage fakes
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

    async def initialize(self):
        return

    async def aclose(self):
        return


# ===========================================================================
# In-memory Workspace + WorkspaceBackend fakes
# ===========================================================================


class _FakeAgentSession:
    def __init__(
        self, *, session_id: str, agent_id: str, workspace_id: str
    ) -> None:
        self.session_id = session_id
        self._agent_id = agent_id
        self._workspace_id = workspace_id
        self._status = SessionStatus.RUNNING
        self.appended: list[str] = []

    async def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            agent_id=self._agent_id,
            workspace_id=self._workspace_id,
            parent_session_id=None,
            status=self._status,
            started_at=datetime.now(timezone.utc),
            last_activity_at=datetime.now(timezone.utc),
        )

    async def status(self) -> SessionStatus:
        return self._status

    async def request_pause(self) -> None:
        self._status = SessionStatus.PAUSED

    async def request_resume(self) -> None:
        self._status = SessionStatus.RUNNING

    async def append_instruction(self, content: str):
        self.appended.append(content)
        from matrix.model.session import Instruction

        return Instruction(
            instruction_id=f"inst-{len(self.appended)}",
            session_id=self.session_id,
            content=content,
            queued_at=datetime.now(timezone.utc),
        )


class _FakeWorkspace:
    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self._files: dict[str, bytes] = {}
        self._sessions: dict[str, _FakeAgentSession] = {}

    @property
    def id(self) -> str:
        return self.workspace_id

    async def list_files(self, path=".", *, recursive=False):
        out: list[FileEntry] = []
        prefix = "" if path in (".", "") else path.rstrip("/") + "/"
        now = datetime.now(timezone.utc)
        for p, content in self._files.items():
            if not p.startswith(prefix):
                continue
            tail = p[len(prefix) :]
            if not recursive and "/" in tail:
                continue
            out.append(
                FileEntry(
                    path=p,
                    kind="file",
                    size_bytes=len(content),
                    modified_at=now,
                )
            )
        return sorted(out, key=lambda fe: fe.path)

    async def file_info(self, path):
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
        if "\x00" in path:
            raise BadRequestError("null byte in path")
        self._files[path] = content

    async def delete_file(self, path):
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        del self._files[path]

    async def log(self, *, limit=50):
        from matrix.model.workspace import CommitInfo

        return [
            CommitInfo(
                sha="a" * 40,
                subject="init",
                committed_at=datetime.now(timezone.utc),
                workspace_id=self.workspace_id,
            )
        ][:limit]

    async def list_sessions(self, *, agent_id=None, status=None):
        out = []
        for s in self._sessions.values():
            out.append(await s.info())
        return out

    async def get_session(self, session_id):
        return self._sessions.get(session_id)

    async def aclose(self):
        return

    def add_session(self, session_id="sess-1", agent_id="agt-1"):
        self._sessions[session_id] = _FakeAgentSession(
            session_id=session_id,
            agent_id=agent_id,
            workspace_id=self.workspace_id,
        )
        return self._sessions[session_id]


class _FakeBackend:
    def __init__(self, _provider) -> None:
        self._workspaces: dict[str, _FakeWorkspace] = {}
        self._counter = 0

    async def initialize(self):
        return

    async def aclose(self):
        for ws in self._workspaces.values():
            await ws.aclose()
        self._workspaces.clear()

    async def create(self, template, *, overrides=None):
        self._counter += 1
        wid = f"ws-{self._counter:04d}"
        ws = _FakeWorkspace(wid)
        self._workspaces[wid] = ws
        return ws

    async def get(self, workspace_id, *, template=None):
        return self._workspaces.get(workspace_id)

    async def list(self):
        return list(self._workspaces.keys())

    async def destroy(self, workspace_id):
        if workspace_id not in self._workspaces:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        await self._workspaces[workspace_id].aclose()
        del self._workspaces[workspace_id]


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def pr(sp) -> ProviderRegistry:
    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


@pytest.fixture
def vsr() -> VectorStoreRegistry:
    cfg = VectorStoreProviderConfig(
        provider=VectorStoreProviderType.PGVECTOR,
        config=PgVectorConfig(
            hostname="x",
            username="u",
            password="p",  # type: ignore[arg-type]
            database="d",
        ),
    )
    return VectorStoreRegistry(cfg, factory=lambda c: object())


@pytest.fixture
def wsr(sp) -> WorkspaceRegistry:
    return WorkspaceRegistry(sp, factory=_FakeBackend)


@pytest.fixture
def app(sp, pr, vsr, wsr):
    return create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
        vector_store_registry=vsr,
        workspace_registry=wsr,
    )


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _provider() -> WorkspaceProvider:
    return WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(path="/tmp/matrix-ws-tests"),
    )


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="tpl-1",
        description="dev workspace",
        provider_id="local-1",
    )


# ===========================================================================
# WorkspaceProvider CRUD (no PUT)
# ===========================================================================


class TestWorkspaceProviderRouter:
    @pytest.mark.asyncio
    async def test_create_then_get_then_delete(self, client) -> None:
        body = _provider().model_dump(mode="json")
        post = await client.post("/v1/workspace_providers", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/workspace_providers/local-1")
        assert get.status_code == 200
        delete = await client.delete("/v1/workspace_providers/local-1")
        assert delete.status_code == 204

    @pytest.mark.asyncio
    async def test_no_put_route(self, client) -> None:
        body = _provider().model_dump(mode="json")
        await client.post("/v1/workspace_providers", json=body)
        put = await client.put("/v1/workspace_providers/local-1", json=body)
        assert put.status_code == 405

    @pytest.mark.asyncio
    async def test_list_paginates(self, client) -> None:
        for i in range(3):
            body = _provider().model_dump(mode="json")
            body["id"] = f"local-{i}"
            await client.post("/v1/workspace_providers", json=body)
        resp = await client.get("/v1/workspace_providers?limit=2&offset=0")
        page = resp.json()
        assert page["length"] == 2
        assert page["total"] == 3


# ===========================================================================
# WorkspaceTemplate CRUD (PUT included)
# ===========================================================================


class TestWorkspaceTemplateRouter:
    @pytest.mark.asyncio
    async def test_full_crud_round_trip(self, client) -> None:
        body = _template().model_dump(mode="json")
        post = await client.post("/v1/workspace_templates", json=body)
        assert post.status_code == 201
        body["description"] = "updated"
        put = await client.put("/v1/workspace_templates/tpl-1", json=body)
        assert put.status_code == 200
        assert put.json()["description"] == "updated"
        delete = await client.delete("/v1/workspace_templates/tpl-1")
        assert delete.status_code == 204


# ===========================================================================
# Workspace CRUD + sub-resources
# ===========================================================================


class TestWorkspaceRouter:
    @pytest.mark.asyncio
    async def test_create_persists_row_and_destroys_on_delete(
        self, client, wsr
    ) -> None:
        await client.post(
            "/v1/workspace_providers", json=_provider().model_dump(mode="json")
        )
        await client.post(
            "/v1/workspace_templates", json=_template().model_dump(mode="json")
        )

        post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
        assert post.status_code == 201, post.text
        body = post.json()
        wid = body["id"]
        assert body["template_id"] == "tpl-1"
        assert body["provider_id"] == "local-1"

        get = await client.get(f"/v1/workspaces/{wid}")
        assert get.status_code == 200

        backend = await wsr.get_backend("local-1")
        assert wid in await backend.list()

        delete = await client.delete(f"/v1/workspaces/{wid}")
        assert delete.status_code == 204
        get2 = await client.get(f"/v1/workspaces/{wid}")
        assert get2.status_code == 404

    @pytest.mark.asyncio
    async def test_create_404_when_template_missing(self, client) -> None:
        await client.post(
            "/v1/workspace_providers", json=_provider().model_dump(mode="json")
        )
        resp = await client.post(
            "/v1/workspaces", json={"template_id": "no-such-template"}
        )
        assert resp.status_code == 404


# ===========================================================================
# Sessions sub-resource
# ===========================================================================


class TestSessionsSubResource:
    async def _setup(self, client, wsr):
        await client.post(
            "/v1/workspace_providers", json=_provider().model_dump(mode="json")
        )
        await client.post(
            "/v1/workspace_templates", json=_template().model_dump(mode="json")
        )
        post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
        wid = post.json()["id"]
        backend = await wsr.get_backend("local-1")
        ws = await backend.get(wid)
        ws.add_session("sess-1", "agt-1")
        return wid

    @pytest.mark.asyncio
    async def test_list_sessions(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        resp = await client.get(f"/v1/workspaces/{wid}/sessions")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_get_session(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        resp = await client.get(f"/v1/workspaces/{wid}/sessions/sess-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == SessionStatus.RUNNING.value

    @pytest.mark.asyncio
    async def test_get_session_404(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        resp = await client.get(f"/v1/workspaces/{wid}/sessions/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_pause_then_resume(self, client, wsr, app, sp) -> None:
        # Pause/resume moved off the workspace sub-resource onto
        # /v1/workspaces/{wid}/sessions/{sid}/{pause,resume} backed by
        # the persisted Session row (Task 20). Seed a Session row so the
        # new endpoints have something to look up.
        from matrix.model.session import (
            AgentSessionBinding,
            Session,
            SessionStatus,
        )

        wid = await self._setup(client, wsr)
        session = Session(
            id="sess-1",
            workspace_id=wid,
            binding=AgentSessionBinding(agent_id="agt-1"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
        await sp.get_storage(Session).create(session)
        pause = await client.post(f"/v1/workspaces/{wid}/sessions/sess-1/pause")
        assert pause.status_code == 204
        resume = await client.post(f"/v1/workspaces/{wid}/sessions/sess-1/resume")
        assert resume.status_code == 200
        assert resume.json()["status"] == "running"

    @pytest.mark.asyncio
    async def test_steer(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        resp = await client.post(
            f"/v1/workspaces/{wid}/sessions/sess-1/steer",
            json={"instruction": "please write tests"},
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "please write tests"


# ===========================================================================
# Files sub-resource
# ===========================================================================


class TestFilesSubResource:
    async def _setup(self, client, wsr):
        await client.post(
            "/v1/workspace_providers", json=_provider().model_dump(mode="json")
        )
        await client.post(
            "/v1/workspace_templates", json=_template().model_dump(mode="json")
        )
        post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
        return post.json()["id"]

    @pytest.mark.asyncio
    async def test_write_then_read_text(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        write = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "hello.txt"},
            json={"content": "hi there", "encoding": "text"},
        )
        assert write.status_code == 204
        read = await client.get(
            f"/v1/workspaces/{wid}/files/read", params={"path": "hello.txt"}
        )
        assert read.status_code == 200
        body = read.json()
        assert body["content"] == "hi there"
        assert body["encoding"] == "text"
        assert body["size_bytes"] == 8

    @pytest.mark.asyncio
    async def test_write_base64_then_read_base64(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        payload = b"\x00\x01\x02\x03binary"
        b64 = base64.b64encode(payload).decode("ascii")
        write = await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "bin.bin"},
            json={"content": b64, "encoding": "base64"},
        )
        assert write.status_code == 204
        read = await client.get(
            f"/v1/workspaces/{wid}/files/read",
            params={"path": "bin.bin", "encoding": "base64"},
        )
        body = read.json()
        assert base64.b64decode(body["content"]) == payload

    @pytest.mark.asyncio
    async def test_list_files(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        for name in ("a.txt", "b.txt"):
            await client.put(
                f"/v1/workspaces/{wid}/files",
                params={"path": name},
                json={"content": "x"},
            )
        resp = await client.get(f"/v1/workspaces/{wid}/files")
        assert resp.status_code == 200
        body = resp.json()
        names = sorted(item["path"] for item in body["items"])
        assert names == ["a.txt", "b.txt"]

    @pytest.mark.asyncio
    async def test_file_info_404(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        resp = await client.get(
            f"/v1/workspaces/{wid}/files/info", params={"path": "missing"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_download(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "data.bin"},
            json={"content": "abcdef"},
        )
        resp = await client.get(
            f"/v1/workspaces/{wid}/files/download", params={"path": "data.bin"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/octet-stream"
        assert "filename=" in resp.headers["content-disposition"]
        assert resp.content == b"abcdef"

    @pytest.mark.asyncio
    async def test_delete(self, client, wsr) -> None:
        wid = await self._setup(client, wsr)
        await client.put(
            f"/v1/workspaces/{wid}/files",
            params={"path": "to-delete"},
            json={"content": "x"},
        )
        delete = await client.delete(
            f"/v1/workspaces/{wid}/files", params={"path": "to-delete"}
        )
        assert delete.status_code == 204
        info = await client.get(
            f"/v1/workspaces/{wid}/files/info", params={"path": "to-delete"}
        )
        assert info.status_code == 404


# ===========================================================================
# Log sub-resource
# ===========================================================================


class TestLogSubResource:
    @pytest.mark.asyncio
    async def test_log(self, client, wsr) -> None:
        await client.post(
            "/v1/workspace_providers", json=_provider().model_dump(mode="json")
        )
        await client.post(
            "/v1/workspace_templates", json=_template().model_dump(mode="json")
        )
        post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
        wid = post.json()["id"]
        resp = await client.get(f"/v1/workspaces/{wid}/log")
        assert resp.status_code == 200
        body = resp.json()
        assert "commits" in body
        assert len(body["commits"]) >= 1


# ===========================================================================
# `_workspaces` toolset wiring through the registry
# ===========================================================================


class TestWorkspacesToolsetRegistration:
    @pytest.mark.asyncio
    async def test_resolves_through_provider_registry(self, app, pr) -> None:
        provider = await pr.get_toolset("_workspaces")
        names = [t.id async for t in provider.list_tools()]
        for name in (
            "list_workspace_providers",
            "create_workspace_template",
            "update_workspace_template",
            "create_workspace",
            "list_workspace_sessions",
            "steer_workspace_session",
            "list_workspace_files",
            "write_workspace_file",
            "get_workspace_log",
        ):
            assert name in names, f"missing {name}"
        assert len(names) == 24

    @pytest.mark.asyncio
    async def test_create_provider_via_toolset(self, app, pr) -> None:
        provider = await pr.get_toolset("_workspaces")
        body = _provider().model_dump(mode="json")
        result = await provider.call(
            tool_name="create_workspace_provider",
            arguments={"entity": body},
        )
        assert not result.is_error, result.output

    @pytest.mark.asyncio
    async def test_invalidate_workspaces_is_noop(self, app, pr) -> None:
        await pr.invalidate_toolset("_workspaces")
        provider = await pr.get_toolset("_workspaces")
        assert provider is app.state.workspaces_toolset
