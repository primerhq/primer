"""Tests for GET /v1/workspaces/{workspace_id}/yields/pending (Studio A3).

Covers:
- Sessions parked on ask_user, watch_files, and _approval return correct items.
- Running / ended sessions are excluded; empty workspace → empty items.
- Parks in another workspace are not returned.
- kind mapping: _approval → "approval"; ask_user / watch_files stay verbatim.
- prompt extraction per kind.
- tool_call_id and parked_at are surfaced correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.storage import OffsetPage, OffsetPageResponse
from primer.model.workspace import (
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


# ===========================================================================
# In-memory storage fakes (minimal; find() filters by predicate naively)
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
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        return OffsetPageResponse(offset=0, length=len(items), total=len(items), items=items)

    async def find(self, predicate, page, *, order_by=None):
        """Evaluate the predicate tree in-memory against every stored entity."""
        from primer.model.storage import Op, Predicate

        def _get_field(obj, name: str):
            """Resolve a (possibly dotted) field path on obj."""
            parts = name.split(".")
            val = obj
            for p in parts:
                if isinstance(val, dict):
                    val = val.get(p)
                else:
                    val = getattr(val, p, None)
            return val

        def _eval(node, obj) -> bool:
            if isinstance(node, Predicate):
                if node.op == Op.AND:
                    return _eval(node.left, obj) and _eval(node.right, obj)
                if node.op == Op.OR:
                    return _eval(node.left, obj) or _eval(node.right, obj)
                # Comparison: left must be FieldRef, right must be Value
                left_val = _get_field(obj, node.left.name)
                right_val = node.right.value
                if node.op == Op.EQ:
                    return left_val == right_val
                if node.op == Op.NE:
                    return left_val != right_val
                if node.op == Op.IN:
                    return left_val in (right_val or [])
                if node.op == Op.IS_NULL:
                    return left_val is None
                if node.op == Op.IS_NOT_NULL:
                    return left_val is not None
                return True
            return True

        matched = [
            item for item in self._data.values()
            if _eval(predicate, item)
        ]
        if isinstance(page, OffsetPage):
            sliced = matched[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(matched),
                items=sliced,
            )
        return OffsetPageResponse(
            offset=0, length=len(matched), total=len(matched), items=matched
        )


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls):
        return self._stores.setdefault(cls, _Storage())

    async def initialize(self):
        return

    async def aclose(self):
        return


class _FakeWorkspace:
    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self._files: dict[str, bytes] = {}
        self._mtimes: dict[str, datetime] = {}
        self._dirs: set[str] = set()
        self._runtime_meta = WorkspaceRuntimeMeta(
            url=f"ws://fake/{workspace_id}",
            token=SecretStr(f"tok-{workspace_id}"),
        )

    @property
    def id(self) -> str:
        return self.workspace_id

    @property
    def runtime_meta(self) -> WorkspaceRuntimeMeta:
        return self._runtime_meta

    async def list_files(self, path=".", *, recursive=False):
        return []

    async def file_info(self, path):
        raise NotFoundError(f"{path!r} not found")

    async def read_file(self, path):
        raise NotFoundError(f"{path!r} not found")

    async def write_file(self, path, content):
        self._files[path] = content
        self._mtimes[path] = datetime.now(timezone.utc)

    async def delete_file(self, path, *, recursive=False):
        raise NotFoundError(f"{path!r} not found")

    async def make_dir(self, path):
        self._dirs.add(path)

    async def list_sessions(self):
        return []

    async def get_session(self, session_id):
        return None

    async def aclose(self):
        return


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

    async def create(
        self, template, *, overrides=None, workspace_id=None, resolvers=None
    ):
        self._counter += 1
        wid = workspace_id or f"ws-{self._counter:04d}"
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


def _provider() -> WorkspaceProvider:
    return WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path="/tmp/primer-ws-yield-tests"),
    )


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="tpl-1",
        description="test workspace",
        provider_id="local-1",
    )


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
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:
            pass
        yield c


# ===========================================================================
# Helpers
# ===========================================================================


async def _create_workspace(client) -> str:
    await client.post("/v1/workspace_providers", json=_provider().model_dump(mode="json"))
    await client.post("/v1/workspace_templates", json=_template().model_dump(mode="json"))
    resp = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_session(
    session_id: str,
    workspace_id: str,
    *,
    parked_status: str | None = None,
    parked_state: dict | None = None,
    parked_at: datetime | None = None,
    status: SessionStatus = SessionStatus.RUNNING,
) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id="agt-1"),
        status=status,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        parked_status=parked_status,
        parked_state=parked_state,
        parked_at=parked_at,
    )


# ===========================================================================
# Tests
# ===========================================================================


class TestListPendingYields:
    @pytest.mark.asyncio
    async def test_empty_workspace_returns_empty_items(self, client, sp) -> None:
        wid = await _create_workspace(client)
        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {"items": []}

    @pytest.mark.asyncio
    async def test_ask_user_park_is_returned(self, client, sp) -> None:
        wid = await _create_workspace(client)
        parked_at = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
        sess = _make_session(
            "sess-ask",
            wid,
            parked_status="parked",
            parked_at=parked_at,
            parked_state={
                "tool_call_id": "tcid-ask-1",
                "yielded": {
                    "tool_name": "ask_user",
                    "event_key": "ask_user:sess-ask:tcid-ask-1",
                    "resume_metadata": {
                        "prompt": "What is your name?",
                    },
                },
            },
        )
        await sp.get_storage(WorkspaceSession).create(sess)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["session_id"] == "sess-ask"
        assert item["kind"] == "ask_user"
        assert item["prompt"] == "What is your name?"
        assert item["tool_call_id"] == "tcid-ask-1"
        assert item["parked_at"] == parked_at.isoformat()

    @pytest.mark.asyncio
    async def test_watch_files_park_is_returned(self, client, sp) -> None:
        wid = await _create_workspace(client)
        sess = _make_session(
            "sess-watch",
            wid,
            parked_status="parked",
            parked_at=datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc),
            parked_state={
                "tool_call_id": "tcid-watch-1",
                "yielded": {
                    "tool_name": "watch_files",
                    "event_key": "watch:sess-watch:tcid-watch-1",
                    "resume_metadata": {
                        "paths": ["src/app.py", "tests/"],
                    },
                },
            },
        )
        await sp.get_storage(WorkspaceSession).create(sess)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["session_id"] == "sess-watch"
        assert item["kind"] == "watch_files"
        assert item["prompt"] == "src/app.py, tests/"
        assert item["tool_call_id"] == "tcid-watch-1"

    @pytest.mark.asyncio
    async def test_approval_park_is_returned_with_kind_approval(self, client, sp) -> None:
        wid = await _create_workspace(client)
        sess = _make_session(
            "sess-approval",
            wid,
            parked_status="parked",
            parked_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
            parked_state={
                "tool_call_id": "tcid-appr-1",
                "yielded": {
                    "tool_name": "_approval",
                    "event_key": "tool_approval:sess-approval:tcid-appr-1",
                    "resume_metadata": {
                        "original_call": {
                            "id": "tcid-appr-1",
                            "name": "bash",
                            "arguments": {"command": "rm -rf /"},
                        },
                    },
                },
            },
        )
        await sp.get_storage(WorkspaceSession).create(sess)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["session_id"] == "sess-approval"
        assert item["kind"] == "approval"
        assert item["prompt"] == "bash"
        assert item["tool_call_id"] == "tcid-appr-1"

    @pytest.mark.asyncio
    async def test_multiple_parks_in_same_workspace(self, client, sp) -> None:
        wid = await _create_workspace(client)
        ask = _make_session(
            "sess-ask-2",
            wid,
            parked_status="parked",
            parked_state={
                "tool_call_id": "tcid-a",
                "yielded": {
                    "tool_name": "ask_user",
                    "event_key": "ask_user:sess-ask-2:tcid-a",
                    "resume_metadata": {"prompt": "Continue?"},
                },
            },
        )
        watch = _make_session(
            "sess-watch-2",
            wid,
            parked_status="parked",
            parked_state={
                "tool_call_id": "tcid-w",
                "yielded": {
                    "tool_name": "watch_files",
                    "event_key": "watch:sess-watch-2:tcid-w",
                    "resume_metadata": {"paths": ["README.md"]},
                },
            },
        )
        await sp.get_storage(WorkspaceSession).create(ask)
        await sp.get_storage(WorkspaceSession).create(watch)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 2
        session_ids = {i["session_id"] for i in items}
        assert session_ids == {"sess-ask-2", "sess-watch-2"}

    @pytest.mark.asyncio
    async def test_running_session_excluded(self, client, sp) -> None:
        wid = await _create_workspace(client)
        running = _make_session(
            "sess-running",
            wid,
            status=SessionStatus.RUNNING,
            # no parked_status
        )
        await sp.get_storage(WorkspaceSession).create(running)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    @pytest.mark.asyncio
    async def test_ended_session_excluded(self, client, sp) -> None:
        wid = await _create_workspace(client)
        ended = _make_session(
            "sess-ended",
            wid,
            status=SessionStatus.ENDED,
        )
        await sp.get_storage(WorkspaceSession).create(ended)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    @pytest.mark.asyncio
    async def test_park_in_other_workspace_not_returned(self, client, sp) -> None:
        wid = await _create_workspace(client)
        # Create a second workspace and park a session there
        resp2 = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
        other_wid = resp2.json()["id"]

        other_sess = _make_session(
            "sess-other-ws",
            other_wid,
            parked_status="parked",
            parked_state={
                "tool_call_id": "tcid-other",
                "yielded": {
                    "tool_name": "ask_user",
                    "event_key": "ask_user:sess-other-ws:tcid-other",
                    "resume_metadata": {"prompt": "Other workspace question"},
                },
            },
        )
        await sp.get_storage(WorkspaceSession).create(other_sess)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    @pytest.mark.asyncio
    async def test_sleep_park_prompt_is_seconds(self, client, sp) -> None:
        wid = await _create_workspace(client)
        sess = _make_session(
            "sess-sleep",
            wid,
            parked_status="parked",
            parked_state={
                "tool_call_id": "tcid-sleep-1",
                "yielded": {
                    "tool_name": "sleep",
                    "event_key": "timer:tcid-sleep-1",
                    "resume_metadata": {"requested_seconds": 30.0},
                },
            },
        )
        await sp.get_storage(WorkspaceSession).create(sess)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["kind"] == "sleep"
        assert items[0]["prompt"] == "30.0s"
        assert items[0]["tool_call_id"] == "tcid-sleep-1"

    @pytest.mark.asyncio
    async def test_parked_at_none_is_null_in_response(self, client, sp) -> None:
        wid = await _create_workspace(client)
        sess = _make_session(
            "sess-no-parked-at",
            wid,
            parked_status="parked",
            parked_at=None,
            parked_state={
                "tool_call_id": "tcid-np",
                "yielded": {
                    "tool_name": "ask_user",
                    "event_key": "ask_user:sess-no-parked-at:tcid-np",
                    "resume_metadata": {"prompt": "Hello?"},
                },
            },
        )
        await sp.get_storage(WorkspaceSession).create(sess)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["parked_at"] is None

    @pytest.mark.asyncio
    async def test_tool_call_id_fallback_from_resume_metadata(self, client, sp) -> None:
        """tool_call_id absent at top level but present inside resume_metadata."""
        wid = await _create_workspace(client)
        sess = _make_session(
            "sess-tcid-fallback",
            wid,
            parked_status="parked",
            parked_state={
                # No top-level tool_call_id — old-format park
                "yielded": {
                    "tool_name": "ask_user",
                    "event_key": "ask_user:sess-tcid-fallback:old-tcid",
                    "resume_metadata": {
                        "prompt": "Old format?",
                        "tool_call_id": "old-tcid",
                    },
                },
            },
        )
        await sp.get_storage(WorkspaceSession).create(sess)

        resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["tool_call_id"] == "old-tcid"
