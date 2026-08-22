"""Tests for :func:`make_crud_router` ``cdc_kind`` parameter.

Since the event-bus cutover (spec 2026-08-22-event-bus-design.md) the
parameter does exactly one thing: register the kind in the global
event registry at factory call time. CRUD events emit from the storage
layer and the seeded ``system-cdc`` subscription converges the system
collection (pinned in ``tests/events/test_cdc_parity.py``). These
tests pin the registration, the ABSENCE of the old imperative hooks,
and that user-supplied hooks still fire.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport
from pydantic import BaseModel

from primer.api.errors import register_error_handlers
from primer.api.routers._cdc_hooks import known_cdc_kinds
from primer.api.routers._crud import make_crud_router
from primer.model.storage import OffsetPage, OffsetPageResponse


class _Agent(BaseModel):
    id: str
    name: str = ""


class _AgentStorage:
    """Just enough Storage surface for the CRUD router."""

    def __init__(self, items: list[_Agent] | None = None) -> None:
        self._items: dict[str, _Agent] = {a.id: a for a in (items or [])}

    async def get(self, id: str, *, conn: Any | None = None) -> _Agent | None:
        return self._items.get(id)

    async def create(self, entity: _Agent, *, conn: Any | None = None) -> _Agent:
        self._items[entity.id] = entity
        return entity

    async def update(self, entity: _Agent, *, conn: Any | None = None) -> _Agent:
        self._items[entity.id] = entity
        return entity

    async def delete(self, id: str, *, conn: Any | None = None) -> None:
        from primer.model.except_ import NotFoundError

        if id not in self._items:
            raise NotFoundError(f"_Agent {id!r} not found")
        del self._items[id]

    async def list(self, page: OffsetPage, *, order_by=None):
        items = list(self._items.values())
        return OffsetPageResponse[
            _Agent
        ](items=items, total=len(items), offset=0, length=len(items))

    async def find(self, predicate, page, *, order_by=None):
        return await self.list(page)


_SHARED_STORAGE: _AgentStorage | None = None


def _get_agent_storage() -> _AgentStorage:
    assert _SHARED_STORAGE is not None
    return _SHARED_STORAGE


class _CDCObserver:
    """Records anything enqueued on a fake internal_collections."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def enqueue(self, event: Any) -> None:
        self.calls.append(event)


# ---------------------------------------------------------------------------
# Registration semantics (unchanged by the cutover)
# ---------------------------------------------------------------------------


def test_cdc_kind_registers_at_factory_call() -> None:
    make_crud_router(
        model_cls=_Agent,
        storage_dep=_get_agent_storage,
        plural="agents_reg",
        tag="agents_reg",
        cdc_kind="cdc_reg_agent",
    )
    assert known_cdc_kinds().get("cdc_reg_agent") is _Agent


def test_cdc_kind_idempotent_same_model() -> None:
    for plural in ("agents_idem_a", "agents_idem_b"):
        make_crud_router(
            model_cls=_Agent,
            storage_dep=_get_agent_storage,
            plural=plural,
            tag=plural,
            cdc_kind="cdc_idem_agent",
        )
    assert known_cdc_kinds().get("cdc_idem_agent") is _Agent


def test_cdc_kind_conflicting_model_raises() -> None:
    class _Other(BaseModel):
        id: str

    make_crud_router(
        model_cls=_Agent,
        storage_dep=_get_agent_storage,
        plural="agents_conf",
        tag="agents_conf",
        cdc_kind="cdc_conf_agent",
    )
    with pytest.raises(ValueError, match="already registered"):
        make_crud_router(
            model_cls=_Other,
            storage_dep=_get_agent_storage,
            plural="agents_conf2",
            tag="agents_conf2",
            cdc_kind="cdc_conf_agent",
        )


# ---------------------------------------------------------------------------
# The imperative hooks are gone; user hooks still fire
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def crud_app() -> AsyncIterator[tuple[httpx.AsyncClient, _CDCObserver, list[str]]]:
    global _SHARED_STORAGE  # noqa: PLW0603

    _SHARED_STORAGE = _AgentStorage(
        items=[_Agent(id="existing", name="pre-seeded")]
    )
    user_calls: list[str] = []

    async def _user_on_create(entity_id: str, request: Request) -> None:
        user_calls.append(entity_id)

    router = make_crud_router(
        model_cls=_Agent,
        storage_dep=_get_agent_storage,
        plural="agents",
        tag="agents",
        cdc_kind="cdc_probe_agent",
        on_create=_user_on_create,
    )

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router, prefix="/v1")

    observer = _CDCObserver()
    app.state.internal_collections = observer

    class _StorageProvider:
        def get_storage(self, model: Any) -> Any:
            return _SHARED_STORAGE

    app.state.storage_provider = _StorageProvider()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, observer, user_calls

    _SHARED_STORAGE = None


@pytest.mark.asyncio
async def test_no_imperative_cdc_side_effects_on_mutations(crud_app) -> None:
    """POST/PUT/DELETE no longer converge inline or feed the dead
    internal-collections queue: the event log + dispatcher own CDC now."""
    client, observer, _ = crud_app
    assert (await client.post(
        "/v1/agents", json={"id": "new-agent", "name": "Alice"},
    )).status_code == 201
    assert (await client.put(
        "/v1/agents/existing", json={"id": "existing", "name": "renamed"},
    )).status_code == 200
    assert (await client.delete("/v1/agents/existing")).status_code == 204
    assert observer.calls == []


@pytest.mark.asyncio
async def test_user_on_create_still_fires(crud_app) -> None:
    client, _, user_calls = crud_app
    resp = await client.post("/v1/agents", json={"id": "combo", "name": "b"})
    assert resp.status_code == 201
    assert "combo" in user_calls
