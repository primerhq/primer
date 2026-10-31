"""Every CRUD and action event names the real person (access-control plan P5 Task 1, Decision 9; spec 6.4).

The event actor is ``user:<user id>`` of the acting person (for a bearer call the token's owner, never the token), and ``system`` for the system
person or when nobody acts (worker internals). The storage layer's CRUD events and ``EventRecorder.emit`` read it; an explicit ``actor=`` still wins.
CRUD writes run on real SQLite storage, whose writer appends the event in the row's transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest_asyncio
from httpx import ASGITransport

from primer.access.acting import acting_as
from primer.access.person import Person
from primer.agent.tool_manager import ToolExecutionManager, invoke_one
from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.auth.api_tokens import extract_prefix, hash_token, mint_plaintext
from primer.events.recorder import EventRecorder, actor_of
from primer.events.registry import register_event_kind
from primer.model.api_token import ApiToken
from primer.model.chat import Tool, ToolCallPart, ToolCallResult
from primer.model.common import Identifiable
from primer.model.principal import Principal, PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider


class EvAttrRow(Identifiable):
    _id_prefix: ClassVar[str | None] = "evattr"
    name: str = ""


def _ref(user_id: str) -> PrincipalRef:
    return PrincipalRef(type="user", id=user_id, display=user_id, role="admin", source="local", user_id=user_id)


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    register_event_kind("evattr", EvAttrRow)
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "attr.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _actors(sp, kind: str) -> list[str]:
    return [e.actor for e in await sp.get_event_store().read_after(0) if e.entity_kind == kind]


class _Writer:
    """A toolset whose one tool writes a row: the write's CRUD event names whoever the call acts for."""

    lists_remotely = False

    def __init__(self, sp) -> None:
        self._sp = sp
        self.count = 0

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None):
        yield Tool(id="write", description="write a row", toolset_id="w", args_schema={"type": "object", "properties": {}})

    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal: str | None = None, ctx=None) -> ToolCallResult:
        self.count += 1
        await self._sp.get_storage(EvAttrRow).create(EvAttrRow(id=f"row-{self.count}", name=tool_name))
        return ToolCallResult(output="ok", is_error=False)


@pytest_asyncio.fixture
async def sqlite_client(sp) -> AsyncIterator[tuple[httpx.AsyncClient, User]]:
    """The test app on the SQLite store, with a registered, signed-in user."""
    registry = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(), cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post("/v1/auth/register", json={"username": "ada", "password": "ada-password-1"})
        assert registered.status_code in (200, 201), registered.text
        user = (await sp.get_storage(User).list(OffsetPage(offset=0, length=10))).items[0]
        yield client, user


def _toolset(toolset_id: str) -> dict:
    return {"id": toolset_id, "provider": "mcp", "config": {"transport": "http", "config": {"url": "http://127.0.0.1:9/mcp"}}}


async def test_rest_create_is_attributed_to_the_user(sp, sqlite_client):
    client, user = sqlite_client

    created = await client.post("/v1/toolsets", params={"allow_unreachable": "true"}, json=_toolset("ts-rest"))

    assert created.status_code == 201, created.text
    events = [e for e in await sp.get_event_store().read_after(0) if e.event_type == "toolset.created" and e.entity_id == "ts-rest"]
    assert [e.actor for e in events] == [f"user:{user.id}"]


async def test_bearer_write_is_attributed_to_the_token_owner(sp, sqlite_client):
    client, user = sqlite_client
    plaintext = mint_plaintext()
    await sp.get_storage(ApiToken).create(ApiToken(
        id="at-ada", user_id=user.id, name="ada", token_hash=hash_token(plaintext), prefix=extract_prefix(plaintext), scopes=["mcp"],
        created_at=datetime.now(UTC),
    ))
    client.cookies.clear()

    created = await client.post(
        "/v1/toolsets", params={"allow_unreachable": "true"}, json=_toolset("ts-bearer"), headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert created.status_code == 201, created.text
    events = [e for e in await sp.get_event_store().read_after(0) if e.event_type == "toolset.created" and e.entity_id == "ts-bearer"]
    assert [e.actor for e in events] == [f"user:{user.id}"], "the token's owner, never the token"


async def test_tool_write_in_a_run_is_the_runs_person(sp):
    manager = ToolExecutionManager(toolset_providers={"w": _Writer(sp)}, initiated_by=_ref("u-run"))

    result = await manager.execute(ToolCallPart(id="c-1", name="w__write", arguments={}))

    assert not result.error, result
    assert await _actors(sp, "evattr") == ["user:u-run"]


async def test_run_person_overrides_an_inherited_request_person(sp):
    """A run's task can inherit the request that spawned it (another user's): the run's own person is who its tools act for."""
    manager = ToolExecutionManager(toolset_providers={"w": _Writer(sp)}, initiated_by=_ref("u-run"))

    with acting_as(Person("u-request", "admin")):
        await manager.list_tools()
        await manager.execute(ToolCallPart(id="c-1", name="w__write", arguments={}))
        await sp.get_storage(EvAttrRow).create(EvAttrRow(id="after", name="the request's own write"))

    assert await _actors(sp, "evattr") == ["user:u-run", "user:u-request"]


async def test_mcp_tool_write_is_the_token_user(sp):
    """The MCP endpoint dispatches through ``invoke_one`` with no run context, under the request's acting person (the token's owner)."""
    with acting_as(Person("u-token-owner", "user")):
        await invoke_one(provider=_Writer(sp), tool_name="write", arguments={}, principal="owner-username")

    assert await _actors(sp, "evattr") == ["user:u-token-owner"]


async def test_mcp_tool_called_event_names_the_user(app, client, fake_storage_provider):
    from tests.api.test_mcp_mount import _mcp_call

    user = (await fake_storage_provider.get_storage(User).list(OffsetPage(offset=0, length=10))).items[0]
    plaintext = mint_plaintext()
    await fake_storage_provider.get_storage(ApiToken).create(ApiToken(
        id="at-mcp", user_id=user.id, name="mcp", token_hash=hash_token(plaintext), prefix=extract_prefix(plaintext), scopes=["mcp"],
        created_at=datetime.now(UTC),
    ))
    assert (await client.put("/v1/mcp_exposure", json={"enabled": True, "allowed_tools": ["misc__uuid_v4"]})).status_code == 200

    result = await _mcp_call(app, headers={"Authorization": f"Bearer {plaintext}"}, tool_name="misc__uuid_v4")

    assert result.is_error is False
    called = [e for e in await fake_storage_provider.get_event_store().read_after(0) if e.event_type == "mcp.tool_called"]
    assert [e.actor for e in called] == [f"user:{user.id}"]


async def test_worker_internal_write_stays_system(sp):
    await sp.get_storage(EvAttrRow).create(EvAttrRow(id="internal", name="nobody acting"))
    with acting_as(Person(None, "system")):
        await sp.get_storage(EvAttrRow).create(EvAttrRow(id="system", name="the system person"))
    recorder = EventRecorder(sp.get_event_store())
    await recorder.emit("session.steered", session_id="s-1")

    assert await _actors(sp, "evattr") == ["system", "system"]
    assert [e.actor for e in await sp.get_event_store().read_after(0) if e.event_type == "session.steered"] == ["system"]


async def test_explicit_actor_still_wins(sp):
    recorder = EventRecorder(sp.get_event_store())
    with acting_as(Person("u-a", "user")):
        await recorder.emit("session.steered", actor="api_token:legacy", session_id="s-1")
        await recorder.emit("session.steered", session_id="s-2")

    steered = [e.actor for e in await sp.get_event_store().read_after(0) if e.event_type == "session.steered"]
    assert steered == ["api_token:legacy", "user:u-a"]
    token = Principal(type="api_token", id="at-1", display="t", role="user", source="internal", user_id="u-owner")
    assert actor_of(token) == "user:u-owner", "a ref that carries a user_id names the user"
    assert actor_of(PrincipalRef(type="trigger", id="tr-1", display="t", source="internal")) == "trigger:tr-1"
    assert actor_of(None) == "system"


# The app fixtures live with the API suite.
from tests.api.conftest import app, client, fake_provider_registry  # noqa: E402, F401
