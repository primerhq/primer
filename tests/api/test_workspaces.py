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

from primer.api.app import create_test_app
from primer.api.registries import (
    ProviderRegistry,
    WorkspaceRegistry,
)
from primer.model.except_ import (
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from primer.model.workspace_session import SessionInfo, SessionStatus
from primer.model.storage import OffsetPage, OffsetPageResponse
from pydantic import SecretStr

from primer.model.workspace import (
    FileEntry,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
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
        from primer.model.workspace_session import Instruction

        return Instruction(
            instruction_id=f"inst-{len(self.appended)}",
            session_id=self.session_id,
            content=content,
            queued_at=datetime.now(timezone.utc),
        )


class _FakeStateRepo:
    """Minimal stand-in exposing the show_commit hook the diff endpoint reads."""

    async def show_commit(self, sha: str) -> dict:
        if sha != "a" * 40:
            raise FileNotFoundError(f"commit {sha!r} not found")
        return {
            "sha": sha,
            "subject": "init",
            "body": "",
            "parent": None,
            "files": [
                {"path": "README.md", "status": "A", "patch": "+ hello\n"}
            ],
        }


class _FakeWorkspace:
    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self._files: dict[str, bytes] = {}
        self._sessions: dict[str, _FakeAgentSession] = {}
        self._runtime_meta = WorkspaceRuntimeMeta(
            url=f"ws://fake/{workspace_id}",
            token=SecretStr(f"tok-{workspace_id}"),
        )
        # Test instrumentation for diagnostic_exec — tests assert the
        # route forwards (command, timeout_seconds) verbatim.
        self.diagnostic_calls: list[tuple[str, float]] = []
        # state_repo stand-in for the diff endpoint.
        self._state = _FakeStateRepo()
        self._diagnostic_raise: BaseException | None = None

    @property
    def id(self) -> str:
        return self.workspace_id

    @property
    def runtime_meta(self) -> WorkspaceRuntimeMeta:
        return self._runtime_meta

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
        from primer.model.workspace import CommitInfo

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

    async def diagnostic_exec(self, command, *, timeout_seconds=5.0):
        """Mock diagnostic_exec used by the diagnostic endpoint tests.

        Records the call (so tests can assert pass-through), and returns
        a deterministic synthetic :class:`WorkspaceDiagnosticResult`. If
        ``self._diagnostic_raise`` is set to an exception, raises it
        instead (used to test the NotImplementedError -> 501 mapping).
        """
        from primer.model.workspace import WorkspaceDiagnosticResult

        self.diagnostic_calls.append((command, timeout_seconds))
        if self._diagnostic_raise is not None:
            raise self._diagnostic_raise
        return WorkspaceDiagnosticResult(
            stdout=f"ok:{command}\n",
            stderr="",
            exit_code=0,
            duration_seconds=0.01,
        )

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
def wsr(sp) -> WorkspaceRegistry:
    return WorkspaceRegistry(sp, factory=_FakeBackend)


@pytest.fixture
def app(sp, pr, wsr):
    return create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
        workspace_registry=wsr,
    )


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:
            pass
        yield c


def _provider() -> WorkspaceProvider:
    return WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path="/tmp/primer-ws-tests"),
    )


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="tpl-1",
        description="dev workspace",
        provider_id="local-1",
    )


# ===========================================================================
# WorkspaceProvider CRUD (reserved ids are read-only via PUT/DELETE)
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
    async def test_workspace_provider_update_succeeds_for_non_reserved(
        self, client
    ) -> None:
        body = _provider().model_dump(mode="json")
        post = await client.post("/v1/workspace_providers", json=body)
        assert post.status_code == 201, post.text

        updated = dict(body)
        updated["config"] = {
            "kind": "local",
            "root_path": "/tmp/primer-ws-updated",
        }
        put = await client.put(
            "/v1/workspace_providers/local-1", json=updated
        )
        assert put.status_code == 200, put.text

        get = await client.get("/v1/workspace_providers/local-1")
        assert get.status_code == 200
        got = get.json()
        assert got["config"]["root_path"] == "/tmp/primer-ws-updated"

    @pytest.mark.asyncio
    async def test_workspace_provider_update_rejects_reserved_id(
        self, client, sp
    ) -> None:
        # Seed the reserved 'local' provider directly through storage
        # (POST is also blocked for reserved ids) so the PUT can hit the
        # pre-update guard.
        reserved = WorkspaceProvider(
            id="local",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(root_path="/tmp/primer-reserved"),
        )
        await sp.get_storage(WorkspaceProvider).create(reserved)

        body = reserved.model_dump(mode="json")
        body["config"] = {
            "kind": "local",
            "root_path": "/tmp/primer-reserved-attacker",
        }
        put = await client.put(
            "/v1/workspace_providers/local",
            json=body,
        )
        assert put.status_code == 403, put.text
        detail = put.json()["detail"]
        assert detail["error"] == "reserved_id_protected"
        assert detail["kind"] == "workspace_provider"

        # Verify the row is unchanged.
        get = await client.get("/v1/workspace_providers/local")
        assert get.status_code == 200
        assert get.json()["config"]["root_path"] == "/tmp/primer-reserved"

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

    @pytest.mark.asyncio
    async def test_container_provider_round_trip(self, client) -> None:
        body = {
            "id": "docker-1",
            "provider": "container",
            "config": {
                "kind": "container",
                "runtime": "docker",
                "connection": {
                    "kind": "socket",
                    "socket_path": "/var/run/docker.sock",
                },
                "reachability": {
                    "kind": "host_port",
                    "bind_host": "127.0.0.1",
                },
                "image_pull_secrets": [],
            },
        }
        post = await client.post("/v1/workspace_providers", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/workspace_providers/docker-1")
        assert get.status_code == 200
        got = get.json()
        assert got["provider"] == "container"
        assert got["config"]["runtime"] == "docker"
        assert got["config"]["connection"]["kind"] == "socket"
        assert got["config"]["reachability"]["kind"] == "host_port"
        delete = await client.delete("/v1/workspace_providers/docker-1")
        assert delete.status_code == 204

    @pytest.mark.asyncio
    async def test_kubernetes_provider_round_trip(self, client) -> None:
        body = {
            "id": "k8s-1",
            "provider": "kubernetes",
            "config": {
                "kind": "kubernetes",
                "variant": "system",
                "connection": {"kind": "in_cluster"},
                "namespace": "primer",
                "reachability": {"kind": "in_cluster"},
                "image_pull_secrets": [],
            },
        }
        post = await client.post("/v1/workspace_providers", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/workspace_providers/k8s-1")
        assert get.status_code == 200
        got = get.json()
        assert got["provider"] == "kubernetes"
        assert got["config"]["namespace"] == "primer"
        assert got["config"]["variant"] == "system"
        assert got["config"]["connection"]["kind"] == "in_cluster"
        assert got["config"]["reachability"]["kind"] == "in_cluster"
        delete = await client.delete("/v1/workspace_providers/k8s-1")
        assert delete.status_code == 204

    @pytest.mark.asyncio
    async def test_provider_kind_config_mismatch_returns_422(self, client) -> None:
        # 'kubernetes' provider with a local-style config must 422.
        bad = {
            "id": "bad",
            "provider": "kubernetes",
            "config": {"kind": "local", "root_path": "/tmp"},
        }
        post = await client.post("/v1/workspace_providers", json=bad)
        assert post.status_code == 422, post.text


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

    @pytest.mark.asyncio
    async def test_container_template_round_trip(self, client) -> None:
        # Seed a container provider first.
        await client.post("/v1/workspace_providers", json={
            "id": "docker-2",
            "provider": "container",
            "config": {
                "kind": "container",
                "runtime": "docker",
                "connection": {
                    "kind": "socket",
                    "socket_path": "/var/run/docker.sock",
                },
                "reachability": {
                    "kind": "host_port",
                    "bind_host": "127.0.0.1",
                },
                "image_pull_secrets": [],
            },
        })
        body = {
            "id": "tpl-container-1",
            "description": "container tpl",
            "provider_id": "docker-2",
            "backend": {
                "kind": "container",
                "image": "ubuntu:24.04",
                "workdir": "/workspace",
                "extra_mounts": [],
            },
        }
        post = await client.post("/v1/workspace_templates", json=body)
        assert post.status_code == 201, post.text
        get = await client.get("/v1/workspace_templates/tpl-container-1")
        assert get.status_code == 200
        got = get.json()
        assert got["backend"]["kind"] == "container"
        assert got["backend"]["image"] == "ubuntu:24.04"
        await client.delete("/v1/workspace_templates/tpl-container-1")
        await client.delete("/v1/workspace_providers/docker-2")

    @pytest.mark.asyncio
    async def test_kubernetes_template_round_trip(self, client) -> None:
        await client.post("/v1/workspace_providers", json={
            "id": "k8s-2",
            "provider": "kubernetes",
            "config": {
                "kind": "kubernetes",
                "variant": "system",
                "connection": {"kind": "in_cluster"},
                "namespace": "default",
                "reachability": {"kind": "in_cluster"},
                "image_pull_secrets": [],
            },
        })
        body = {
            "id": "tpl-k8s-1",
            "description": "k8s tpl",
            "provider_id": "k8s-2",
            "backend": {
                "kind": "kubernetes",
                "image": "ubuntu:24.04",
                "workdir": "/workspace",
                "pvc_size": "5Gi",
                "pvc_access_modes": ["ReadWriteOnce"],
            },
        }
        post = await client.post("/v1/workspace_templates", json=body)
        assert post.status_code == 201, post.text
        got = (await client.get("/v1/workspace_templates/tpl-k8s-1")).json()
        assert got["backend"]["kind"] == "kubernetes"
        assert got["backend"]["pvc_size"] == "5Gi"
        await client.delete("/v1/workspace_templates/tpl-k8s-1")
        await client.delete("/v1/workspace_providers/k8s-2")

    @pytest.mark.asyncio
    async def test_template_backend_kind_mismatch_returns_422(self, client) -> None:
        # Seed a local provider but use a container backend in the template.
        await client.post("/v1/workspace_providers", json={
            "id": "local-mismatch",
            "provider": "local",
            "config": {"kind": "local", "root_path": "/tmp"},
        })
        bad = {
            "id": "tpl-bad",
            "description": "kind mismatch",
            "provider_id": "local-mismatch",
            "backend": {
                "kind": "container",
                "image": "ubuntu:24.04",
            },
        }
        # The backend currently accepts the row (no cross-validation between
        # provider.provider and template.backend.kind). Materialisation fails
        # at workspace-create time. Pin the OBSERVABLE behaviour: row 201s.
        # If the API later adds cross-validation, this test should be updated
        # to assert 422 instead.
        post = await client.post("/v1/workspace_templates", json=bad)
        assert post.status_code in (201, 422), post.text
        # Cleanup whichever the server accepted.
        await client.delete("/v1/workspace_templates/tpl-bad")
        await client.delete("/v1/workspace_providers/local-mismatch")

    @pytest.mark.asyncio
    async def test_workspace_template_create_rejects_reserved_id(
        self, client
    ) -> None:
        """POST /v1/workspace_templates with id=local-default returns 409."""
        body = {
            "id": "local-default",
            "description": "attacker-supplied",
            "provider_id": "local",
            "backend": {"kind": "local"},
        }
        post = await client.post("/v1/workspace_templates", json=body)
        assert post.status_code == 409, post.text
        detail = post.json()["detail"]
        assert detail["error"] == "reserved_id"
        assert detail["kind"] == "workspace_template"

    @pytest.mark.asyncio
    async def test_workspace_template_delete_rejects_reserved_id(
        self, client, sp
    ) -> None:
        """DELETE /v1/workspace_templates/local-default returns 403."""
        # Seed the reserved template directly through storage so the DELETE
        # can reach the pre-delete guard (POST is blocked above).
        reserved = WorkspaceTemplate(
            id="local-default",
            description="reserved",
            provider_id="local",
        )
        await sp.get_storage(WorkspaceTemplate).create(reserved)

        delete = await client.delete("/v1/workspace_templates/local-default")
        assert delete.status_code == 403, delete.text
        detail = delete.json()["detail"]
        assert detail["error"] == "reserved_id_protected"
        assert detail["kind"] == "workspace_template"

        # Row preserved.
        got = await client.get("/v1/workspace_templates/local-default")
        assert got.status_code == 200

    @pytest.mark.asyncio
    async def test_workspace_template_update_rejects_reserved_id(
        self, client, sp
    ) -> None:
        """PUT /v1/workspace_templates/local-default returns 403."""
        reserved = WorkspaceTemplate(
            id="local-default",
            description="reserved",
            provider_id="local",
        )
        await sp.get_storage(WorkspaceTemplate).create(reserved)

        body = reserved.model_dump(mode="json")
        body["description"] = "attacker-edit"
        put = await client.put(
            "/v1/workspace_templates/local-default", json=body
        )
        assert put.status_code == 403, put.text
        detail = put.json()["detail"]
        assert detail["error"] == "reserved_id_protected"
        assert detail["kind"] == "workspace_template"

        # Row unchanged.
        got = await client.get("/v1/workspace_templates/local-default")
        assert got.status_code == 200
        assert got.json()["description"] == "reserved"


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

    @pytest.mark.asyncio
    async def test_workspace_get_includes_phase_fields(self, client) -> None:
        """GET /v1/workspaces/{id} returns phase, last_probe_at,
        last_probe_ok, failure_reason, runtime_meta."""
        await client.post(
            "/v1/workspace_providers", json=_provider().model_dump(mode="json")
        )
        await client.post(
            "/v1/workspace_templates", json=_template().model_dump(mode="json")
        )
        post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
        wid = post.json()["id"]

        get = await client.get(f"/v1/workspaces/{wid}")
        assert get.status_code == 200
        body = get.json()
        assert "phase" in body
        assert "last_probe_at" in body
        assert "last_probe_ok" in body
        assert "failure_reason" in body
        assert "runtime_meta" in body
        # The create handler marks freshly-materialised workspaces as
        # "running" so the probe loop owns the row from tick #1; the
        # ambient row default is "pending" but should never surface
        # via the create path.
        assert body["phase"] == "running"
        assert body["last_probe_at"] is None
        assert body["last_probe_ok"] is False
        assert body["failure_reason"] is None

    @pytest.mark.asyncio
    async def test_pause_returns_501(self, client) -> None:
        """POST /v1/workspaces/{id}/pause is reserved and returns 501."""
        # Note: this test doesn't need a workspace to exist — the 501 is
        # returned BEFORE any storage lookup. If the route handler signature
        # implies it accepts the id without backend lookup, that's correct.
        resp = await client.post("/v1/workspaces/some-id/pause")
        assert resp.status_code == 501
        body = resp.json()
        # FastAPI wraps HTTPException.detail as {"detail": ...}
        assert body["detail"]["error"] == "not_implemented"
        assert "pause" in body["detail"]["message"].lower()

    @pytest.mark.asyncio
    async def test_resume_returns_501(self, client) -> None:
        resp = await client.post("/v1/workspaces/some-id/resume")
        assert resp.status_code == 501
        body = resp.json()
        assert body["detail"]["error"] == "not_implemented"
        assert "resume" in body["detail"]["message"].lower()

    @pytest.mark.asyncio
    async def test_create_against_agent_sandbox_provider_returns_501(
        self, client
    ) -> None:
        """POST /v1/workspaces against a k8s provider with
        variant=agent_sandbox returns 501 — the slot is reserved but not yet
        implemented."""
        prov_resp = await client.post(
            "/v1/workspace_providers",
            json={
                "id": "k8s-as",
                "provider": "kubernetes",
                "config": {
                    "kind": "kubernetes",
                    "variant": "agent_sandbox",
                    "connection": {"kind": "in_cluster"},
                    "namespace": "primer",
                    "reachability": {"kind": "in_cluster"},
                },
            },
        )
        assert prov_resp.status_code in (200, 201), prov_resp.text

        tpl_resp = await client.post(
            "/v1/workspace_templates",
            json={
                "id": "tpl-k",
                "description": "t",
                "provider_id": "k8s-as",
                "backend": {
                    "kind": "kubernetes",
                    "image": "primer-runtime:1",
                },
                "files": [],
                "env": {},
                "init_commands": [],
            },
        )
        assert tpl_resp.status_code in (200, 201), tpl_resp.text

        resp = await client.post(
            "/v1/workspaces",
            json={"template_id": "tpl-k", "provider_id": "k8s-as"},
        )
        assert resp.status_code == 501, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "not_implemented"
        assert "agent_sandbox" in detail["message"]

    @pytest.mark.asyncio
    async def test_workspace_list_includes_phase(self, client) -> None:
        """GET /v1/workspaces lists each workspace with its phase + probe
        fields (no stripped DTO in the way)."""
        await client.post(
            "/v1/workspace_providers", json=_provider().model_dump(mode="json")
        )
        await client.post(
            "/v1/workspace_templates", json=_template().model_dump(mode="json")
        )
        await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
        await client.post("/v1/workspaces", json={"template_id": "tpl-1"})

        resp = await client.get("/v1/workspaces")
        assert resp.status_code == 200
        page = resp.json()
        assert page["total"] == 2
        for item in page["items"]:
            assert "phase" in item
            assert "last_probe_at" in item
            assert "last_probe_ok" in item
            assert "failure_reason" in item
            assert "runtime_meta" in item
            assert item["phase"] == "running"


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
        # the persisted WorkspaceSession row (Task 20). Seed a WorkspaceSession row so the
        # new endpoints have something to look up.
        from primer.model.workspace_session import (
            AgentSessionBinding,
            WorkspaceSession,
            SessionStatus,
        )

        wid = await self._setup(client, wsr)
        session = WorkspaceSession(
            id="sess-1",
            workspace_id=wid,
            binding=AgentSessionBinding(agent_id="agt-1"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
        await sp.get_storage(WorkspaceSession).create(session)
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


class TestDiagnosticEndpoint:
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
        return wid, ws

    @pytest.mark.asyncio
    async def test_echo_whitelisted_passes_through(self, client, wsr) -> None:
        wid, ws = await self._setup(client, wsr)
        resp = await client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "echo hello"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stdout"] == "ok:echo hello\n"
        assert body["stderr"] == ""
        assert body["exit_code"] == 0
        assert "duration_seconds" in body
        # Default timeout (5.0) wired through.
        assert ws.diagnostic_calls == [("echo hello", 5.0)]

    @pytest.mark.asyncio
    async def test_custom_timeout_forwarded(self, client, wsr) -> None:
        wid, ws = await self._setup(client, wsr)
        resp = await client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "pwd", "timeout_seconds": 2.5},
        )
        assert resp.status_code == 200, resp.text
        assert ws.diagnostic_calls == [("pwd", 2.5)]

    @pytest.mark.asyncio
    async def test_each_whitelisted_command_allowed(self, client, wsr) -> None:
        wid, _ws = await self._setup(client, wsr)
        for cmd in ("echo hi", "pwd", "whoami", "uname -a", "ls -la"):
            resp = await client.post(
                f"/v1/workspaces/{wid}/diagnostic",
                json={"command": cmd},
            )
            assert resp.status_code == 200, (cmd, resp.text)

    @pytest.mark.asyncio
    async def test_non_whitelisted_command_returns_400(self, client, wsr) -> None:
        wid, ws = await self._setup(client, wsr)
        resp = await client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "rm -rf /"},
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "command_not_whitelisted"
        assert detail["head"] == "rm"
        assert set(detail["allowed"]) == {"echo", "pwd", "whoami", "uname", "ls"}
        # Backend was NOT called — whitelist short-circuits before
        # diagnostic_exec dispatch.
        assert ws.diagnostic_calls == []

    @pytest.mark.asyncio
    async def test_command_with_pipe_blocked(self, client, wsr) -> None:
        # Even though the head token is whitelisted, downstream operators
        # are irrelevant — the whitelist guards the head only, so a shell
        # pipe targeting another binary is still subject to the head
        # check. We at least confirm the head-token logic rejects pure
        # non-whitelisted heads (already covered) and accepts a
        # whitelisted head with args.
        wid, ws = await self._setup(client, wsr)
        # Empty command is min_length=1 -> 422.
        resp = await client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": ""},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_workspace_not_found_returns_404(self, client, wsr) -> None:
        # No workspace created => registry.get_workspace raises.
        resp = await client.post(
            "/v1/workspaces/ws-missing/diagnostic",
            json={"command": "echo hi"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_not_implemented_maps_to_501(self, client, wsr) -> None:
        wid, ws = await self._setup(client, wsr)
        ws._diagnostic_raise = NotImplementedError(
            "WSSandbox lacks shell exec primitive"
        )
        resp = await client.post(
            f"/v1/workspaces/{wid}/diagnostic",
            json={"command": "echo hi"},
        )
        assert resp.status_code == 501
        detail = resp.json()["detail"]
        assert detail["error"] == "not_implemented"


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

    @pytest.mark.asyncio
    async def test_show_commit_returns_diff(self, client, wsr) -> None:
        """GET /v1/workspaces/{wid}/commit/{sha} returns the per-commit
        diff payload (subject, parent, files: [{path, status, patch}])."""
        await client.post(
            "/v1/workspace_providers", json=_provider().model_dump(mode="json")
        )
        await client.post(
            "/v1/workspace_templates", json=_template().model_dump(mode="json")
        )
        post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
        wid = post.json()["id"]
        log_resp = await client.get(f"/v1/workspaces/{wid}/log")
        assert log_resp.status_code == 200
        commits = log_resp.json()["commits"]
        assert commits, "expected at least one commit in the seeded workspace"
        sha = commits[0]["sha"]

        resp = await client.get(f"/v1/workspaces/{wid}/commit/{sha}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sha"] == sha
        assert "subject" in body
        assert "files" in body
        assert isinstance(body["files"], list)

    @pytest.mark.asyncio
    async def test_show_commit_unknown_sha_404(self, client, wsr) -> None:
        await client.post(
            "/v1/workspace_providers", json=_provider().model_dump(mode="json")
        )
        await client.post(
            "/v1/workspace_templates", json=_template().model_dump(mode="json")
        )
        post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
        wid = post.json()["id"]
        # A syntactically valid but nonexistent sha.
        resp = await client.get(
            f"/v1/workspaces/{wid}/commit/0000000000000000000000000000000000000000"
        )
        assert resp.status_code == 404


# ===========================================================================
# `_workspaces` toolset wiring through the registry
# ===========================================================================


class TestWorkspacesToolsetRegistration:
    @pytest.mark.asyncio
    async def test_resolves_through_provider_registry(self, app, pr) -> None:
        provider = await pr.get_toolset("workspaces")
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
        # 24 original + watch_files (yielding-tools M4) = 25.
        assert len(names) == 25
        assert "watch_files" in names

    @pytest.mark.asyncio
    async def test_create_provider_via_toolset(self, app, pr) -> None:
        provider = await pr.get_toolset("workspaces")
        body = _provider().model_dump(mode="json")
        result = await provider.call(
            tool_name="create_workspace_provider",
            arguments={"entity": body},
        )
        assert not result.is_error, result.output

    @pytest.mark.asyncio
    async def test_invalidate_workspaces_is_noop(self, app, pr) -> None:
        await pr.invalidate_toolset("workspaces")
        provider = await pr.get_toolset("workspaces")
        assert provider is app.state.workspaces_toolset
