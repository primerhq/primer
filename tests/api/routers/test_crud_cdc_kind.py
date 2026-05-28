"""Tests for :func:`make_crud_router` ``cdc_kind`` parameter.

Verifies that when ``cdc_kind`` is set the factory:
- Registers the kind in :func:`known_cdc_kinds` at factory call time.
- Auto-wires CDC hooks so they fire on create / update / delete.
- Composes CDC hooks with user-supplied on_create / on_update / on_delete
  (both fire).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport
from pydantic import BaseModel

from primer.api.errors import register_error_handlers
from primer.api.routers._crud import make_crud_router
from primer.model.storage import OffsetPage, OffsetPageResponse


# ---------------------------------------------------------------------------
# Minimal model used by all tests in this file
# ---------------------------------------------------------------------------


class _Agent(BaseModel):
    id: str
    name: str = ""


# ---------------------------------------------------------------------------
# In-memory storage for _Agent
# ---------------------------------------------------------------------------


class _AgentStorage:
    def __init__(self, items: list[_Agent] | None = None) -> None:
        self._data: dict[str, _Agent] = {item.id: item for item in (items or [])}

    async def get(self, id: str) -> _Agent | None:
        return self._data.get(id)

    async def create(self, entity: _Agent) -> _Agent:
        self._data[entity.id] = entity
        return entity

    async def update(self, entity: _Agent) -> _Agent:
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        self._data.pop(id, None)

    async def list(self, page: Any, *, order_by: Any = None) -> OffsetPageResponse[_Agent]:
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

    async def find(self, predicate: Any, page: Any, *, order_by: Any = None) -> OffsetPageResponse[_Agent]:
        return await self.list(page, order_by=order_by)


# ---------------------------------------------------------------------------
# Fixture: reset CDC registry around every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cdc() -> Any:  # type: ignore[return]
    from primer.api.routers._cdc_hooks import _reset_for_test

    _reset_for_test()
    yield
    _reset_for_test()


# ---------------------------------------------------------------------------
# Unit test: registration happens at factory call time
# ---------------------------------------------------------------------------


def test_cdc_kind_registers_at_factory_call() -> None:
    """Calling make_crud_router with cdc_kind= registers the kind immediately."""
    from primer.api.routers._cdc_hooks import known_cdc_kinds

    storage = _AgentStorage()

    make_crud_router(
        model_cls=_Agent,
        storage_dep=lambda: storage,
        plural="agents",
        tag="agents",
        cdc_kind="agent",
    )

    assert "agent" in known_cdc_kinds()
    assert known_cdc_kinds()["agent"] is _Agent


# ---------------------------------------------------------------------------
# Unit test: duplicate registration with same model is idempotent
# ---------------------------------------------------------------------------


def test_cdc_kind_idempotent_same_model() -> None:
    """Re-registering the same kind + model is a no-op (safe on re-import)."""
    from primer.api.routers._cdc_hooks import known_cdc_kinds

    storage = _AgentStorage()

    make_crud_router(
        model_cls=_Agent,
        storage_dep=lambda: storage,
        plural="agents1",
        tag="agents1",
        cdc_kind="agent",
    )
    # Second call — same kind, same model_cls — must not raise.
    make_crud_router(
        model_cls=_Agent,
        storage_dep=lambda: storage,
        plural="agents2",
        tag="agents2",
        cdc_kind="agent",
    )

    assert known_cdc_kinds()["agent"] is _Agent


# ---------------------------------------------------------------------------
# Shared app + storage singleton used by HTTP integration tests
# ---------------------------------------------------------------------------

_SHARED_STORAGE: _AgentStorage | None = None


def _get_agent_storage() -> _AgentStorage:
    assert _SHARED_STORAGE is not None
    return _SHARED_STORAGE


# ---------------------------------------------------------------------------
# Observer helper: captures (op, kind, entity_id) tuples enqueued to IC
# ---------------------------------------------------------------------------


class _CDCObserver:
    """Records every IngestEvent enqueued against a fake IC subsystem."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def enqueue(self, event: Any) -> None:
        self.calls.append((event.op, event.entity_type, event.entity_id))


# ---------------------------------------------------------------------------
# Fixtures for HTTP integration tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cdc_client_and_observer() -> AsyncIterator[tuple[httpx.AsyncClient, _CDCObserver]]:
    """Yields (client, observer) with CDC hooks wired to an in-memory observer."""
    global _SHARED_STORAGE  # noqa: PLW0603

    _SHARED_STORAGE = _AgentStorage(
        items=[_Agent(id="existing", name="pre-seeded")]
    )

    router = make_crud_router(
        model_cls=_Agent,
        storage_dep=_get_agent_storage,
        plural="agents",
        tag="agents",
        cdc_kind="agent",
    )

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router, prefix="/v1")

    observer = _CDCObserver()
    # Attach observer as request.app.state.internal_collections
    app.state.internal_collections = observer
    # The CDC hook also reads storage_provider from app.state to fetch the
    # entity; provide a minimal stub so it can look up the agent.
    class _StorageProvider:
        def get_storage(self, model: Any) -> Any:
            return _SHARED_STORAGE

    app.state.storage_provider = _StorageProvider()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, observer

    _SHARED_STORAGE = None


# ---------------------------------------------------------------------------
# Integration tests: hooks fire on HTTP mutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdc_hook_fires_on_create(
    cdc_client_and_observer: tuple[httpx.AsyncClient, _CDCObserver],
) -> None:
    """POST /v1/agents should trigger an 'upsert' CDC event."""
    client, observer = cdc_client_and_observer
    resp = await client.post("/v1/agents", json={"id": "new-agent", "name": "Alice"})
    assert resp.status_code == 201

    # CDC hook enqueues an upsert event with the new entity's id.
    ops = [call for call in observer.calls if call[2] == "new-agent"]
    assert len(ops) >= 1
    assert ops[0][0] == "upsert"
    assert ops[0][1] == "agent"


@pytest.mark.asyncio
async def test_cdc_hook_fires_on_update(
    cdc_client_and_observer: tuple[httpx.AsyncClient, _CDCObserver],
) -> None:
    """PUT /v1/agents/{id} should trigger an 'upsert' CDC event."""
    client, observer = cdc_client_and_observer
    resp = await client.put(
        "/v1/agents/existing",
        json={"id": "existing", "name": "renamed"},
    )
    assert resp.status_code == 200

    ops = [call for call in observer.calls if call[2] == "existing"]
    assert len(ops) >= 1
    assert ops[0][0] == "upsert"
    assert ops[0][1] == "agent"


@pytest.mark.asyncio
async def test_cdc_hook_fires_on_delete(
    cdc_client_and_observer: tuple[httpx.AsyncClient, _CDCObserver],
) -> None:
    """DELETE /v1/agents/{id} should trigger a 'delete' CDC event."""
    client, observer = cdc_client_and_observer
    resp = await client.delete("/v1/agents/existing")
    assert resp.status_code == 204

    ops = [call for call in observer.calls if call[2] == "existing"]
    assert len(ops) >= 1
    assert ops[0][0] == "delete"
    assert ops[0][1] == "agent"


# ---------------------------------------------------------------------------
# Integration test: CDC hooks compose with user-supplied on_create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdc_and_user_hooks_both_fire_on_create() -> None:
    """When both cdc_kind and on_create are supplied both fire on POST."""
    global _SHARED_STORAGE  # noqa: PLW0603

    _SHARED_STORAGE = _AgentStorage()

    user_calls: list[str] = []

    async def _user_on_create(entity_id: str, request: Request) -> None:
        user_calls.append(entity_id)

    router = make_crud_router(
        model_cls=_Agent,
        storage_dep=_get_agent_storage,
        plural="agents3",
        tag="agents3",
        cdc_kind="agent",
        on_create=_user_on_create,
    )

    observer = _CDCObserver()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router, prefix="/v1")
    app.state.internal_collections = observer

    class _SP:
        def get_storage(self, model: Any) -> Any:
            return _SHARED_STORAGE

    app.state.storage_provider = _SP()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/agents3", json={"id": "combo", "name": "both"})

    assert resp.status_code == 201
    assert "combo" in user_calls
    cdc_ops = [c for c in observer.calls if c[2] == "combo"]
    assert len(cdc_ops) >= 1

    _SHARED_STORAGE = None


# ---------------------------------------------------------------------------
# Integration test: no IC subsystem — hooks are silent no-ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdc_hooks_noop_without_ic_subsystem() -> None:
    """When internal_collections is not on app.state, CDC hooks should not raise."""
    global _SHARED_STORAGE  # noqa: PLW0603

    _SHARED_STORAGE = _AgentStorage()

    router = make_crud_router(
        model_cls=_Agent,
        storage_dep=_get_agent_storage,
        plural="agents4",
        tag="agents4",
        cdc_kind="agent",
    )

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router, prefix="/v1")
    # Deliberately do NOT attach internal_collections to app.state

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/agents4", json={"id": "silent", "name": "no-ic"})

    assert resp.status_code == 201

    _SHARED_STORAGE = None
