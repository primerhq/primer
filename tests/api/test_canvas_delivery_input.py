"""What a user entered on a canvas stays in its session's own input, whichever way the event is delivered (pivot P5 S5, the lead's ruling 1).

Sessions and their rows are a shared read (the user's G7-02 decision): ``GET /v1/sessions/{id}`` serves any signed-in user the row, its
``parked_state`` and its queued steers, and ``GET /v1/events`` serves a non-admin the canvas events without their input (#872). The
delivery (``primer.canvas.delivery``) adds copies of each event: the queued steer's attribution, the USER_INPUT's ``payload.canvas_event``
and ``payload.canvas_digest``, the ``canvas_event`` record, what a wake stamps on the row or publishes. None of them may carry a click's
``value`` or an Html emit's ``data``; only the TEXT the owning session's agent reads does (the ``<canvas_event>`` block and the
``<canvas_changes>`` digest), and a queued steer's text is that block.

Each path the delivery takes (``wait``, ``woken``, ``queued``, ``recorded``) runs on the real SQLite storage, with the real
``CanvasService``, delivery, ``wake_session``, queue, record writer and, for ``wait``, the real ``canvas_wait`` tool, dispatcher and
flip. Another signed-in non-admin then reads the session and the event feed through the real routers. The workspace (the session's
``messages.jsonl`` and slot) is the canvas suite's recording fake, which is where the session's own input text is read back.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.auth.passwords import hash_password
from primer.canvas.delivery import (
    CANVAS_DIGEST_PAYLOAD_KEY,
    CANVAS_EVENT_PAYLOAD_KEY,
    DELIVERY_QUEUED,
    DELIVERY_RECORDED,
    DELIVERY_WAIT,
    DELIVERY_WOKEN,
    deliver_canvas_event,
)
from primer.canvas.lang import parse_program
from primer.canvas.service import CanvasService
from primer.events.dispatcher import EventDispatcher
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.pending_messages import realize_next_pending
from primer.session.yields import flip_sessions_parked_on
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.canvas import build_canvas_toolset
from tests.canvas._fakes import FakeBus, FakeRegistry

BODY_TIMEOUT_S = 60

WORKSPACE, SESSION = "ws-prime", "s-admin"
ADMIN_REF = PrincipalRef(type="user", id="u-admin", display="evadmin", role="admin", source="local")
INPUT = ("value", "data")

PROGRAM = """
root = Column([controls, viz])
controls = Row([genre, play])
genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
play = Button("Play", "primary")
viz = Html("<div id=b>pick one</div>", 60)
"""
#: What the user entered: distinctive, so a copy shows wherever it leaked.
SECRET_NOTE = "the owner's private note 7f3a"
CHOSEN = "Classical"
EMIT = {"element": "viz", "kind": "emit", "name": "submit", "data": {"note": SECRET_NOTE}}
CHANGE = {"element": "genre", "kind": "change", "value": CHOSEN}


class _Scheduler:
    async def enqueue(self, session_id: str) -> None:
        return None


class _Engine:
    async def upsert(self, *_: Any, **__: Any) -> None:
        return None


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "delivery.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest_asyncio.fixture
async def reader(sp: SqliteStorageProvider) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in non-admin who neither started the session nor clicked: another user of the shared install."""
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as boot, \
            httpx.AsyncClient(transport=transport, base_url="http://test") as other:
        reg = await boot.post("/v1/auth/register", json={"username": "evadmin", "password": "evadminpass1"})
        assert reg.status_code == 200, reg.text
        await sp.get_storage(User).create(User(
            id="u-other", username="other", password_hash=await hash_password("otherpass1"), created_at=datetime.now(UTC), role="user",
        ))
        login = await other.post("/v1/auth/login", json={"username": "other", "password": "otherpass1"})
        assert login.status_code == 200, login.text
        yield other


class _World:
    def __init__(self, sp: SqliteStorageProvider) -> None:
        self.sp = sp
        self.bus = FakeBus()
        self.registry = FakeRegistry()
        self.service = CanvasService(sp, event_bus=self.bus, workspace_registry=self.registry, script_check=lambda body: [])
        self.base = 0

    @property
    def sessions(self) -> Any:
        return self.sp.get_storage(WorkspaceSession)

    async def seed(self, **row: Any) -> None:
        self.base = await self.sp.get_event_store().max_id()
        await self.sessions.create(WorkspaceSession(
            id=SESSION, workspace_id=WORKSPACE, binding=AgentSessionBinding(agent_id="operator"), created_at=datetime.now(UTC),
            initiated_by=ADMIN_REF, **({"status": SessionStatus.WAITING, "turn_status": "idle"} | row),
        ))
        await self.service.apply(
            session_id=SESSION, workspace_id=WORKSPACE, statements=parse_program(PROGRAM), unit="line", base_rev=None, source="agent",
            by="agent",
        )

    def deps(self) -> SessionWakeDeps:
        return SessionWakeDeps(storage_provider=self.sp, scheduler=_Scheduler(), claim_engine=_Engine(), workspace_registry=self.registry,
                               event_bus=self.bus)

    async def interact(self, interaction: dict[str, Any]) -> str:
        event = await self.service.record_event(session_id=SESSION, workspace_id=WORKSPACE, by="evadmin", **interaction)
        row = await self.service.current(SESSION)
        return await deliver_canvas_event(
            event=event, state=self.service.state_of(row), session_id=SESSION, workspace_id=WORKSPACE, storage_provider=self.sp,
            scheduler=_Scheduler(), claim_engine=_Engine(), workspace_registry=self.registry, event_bus=self.bus,
        )

    def records(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.registry.workspace(WORKSPACE).lines(SESSION) if kind is None or r["kind"] == kind]

    def own_input_texts(self) -> list[str]:
        """The session's own input: every instruction its slot was handed, with the parts that rode it (the digest)."""
        slot = self.registry.workspace(WORKSPACE).slots.get(SESSION)
        return [text + "\n" + "\n".join(p.text for p in parts) for text, parts in (slot.appended if slot else [])]

    async def end_turn(self) -> None:
        await self.sessions.update((await self.sessions.get(SESSION)).model_copy(update={"turn_status": "idle"}))


def _keys(blob: Any) -> set[str]:
    """Every key of every mapping anywhere in ``blob``."""
    if isinstance(blob, dict):
        return set(blob) | {k for v in blob.values() for k in _keys(v)}
    if isinstance(blob, list):
        return {k for v in blob for k in _keys(v)}
    return set()


def _without_queued_text(row: dict[str, Any]) -> dict[str, Any]:
    """The served row with each queued steer's TEXT taken out: a queued click's text is its block, the session's own next input."""
    pending = [{**p, "parts": [{k: v for k, v in part.items() if k != "text"} for part in p["parts"]]} for p in row["pending_messages"]]
    return {**row, "pending_messages": pending}


async def _assert_the_shared_reads_hold_no_input(reader: httpx.AsyncClient, w: _World) -> dict[str, Any]:
    """``GET /v1/sessions/{id}`` and ``GET /v1/events`` as another non-admin: no ``value`` or ``data`` key anywhere, and the entered text
    nowhere but in a queued steer's text. Returns the served row."""
    resp = await reader.get(f"/v1/sessions/{SESSION}")
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert not _keys(row) & set(INPUT), f"another user reads an input key on the session: {sorted(_keys(row) & set(INPUT))}"
    structured = json.dumps(_without_queued_text(row))
    assert SECRET_NOTE not in structured and CHOSEN not in structured, structured

    resp = await reader.get(f"/v1/events?after_id={w.base}&workspace_id={WORKSPACE}&limit=200")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [e for e in items if e["event_type"] == "canvas.event"], "the feed serves the session's canvas events"
    assert not _keys([e["payload"] for e in items]) & set(INPUT), "the feed serves a non-admin no input key"
    assert SECRET_NOTE not in json.dumps(items) and CHOSEN not in json.dumps(items)
    return row


def _assert_the_copies_hold_no_input(w: _World) -> None:
    """The delivery's own copies: every record's payload but the USER_INPUT text, and every bus publish."""
    copies = [
        {k: v for k, v in r["payload"].items() if k != "text"} for r in w.records() if r["kind"] != "canvas_op"
    ] + [payload for _key, payload in w.bus.published]
    assert not _keys(copies) & set(INPUT), copies
    assert SECRET_NOTE not in json.dumps(copies, default=str) and CHOSEN not in json.dumps(copies, default=str)


async def test_woken(sp, reader) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        w = _World(sp)
        await w.seed()
        assert await w.interact(CHANGE) == DELIVERY_RECORDED            # a silent change first: the digest of the wake lists it
        assert await w.interact(EMIT) == DELIVERY_WOKEN

        row = await _assert_the_shared_reads_hold_no_input(reader, w)
        assert row["turn_status"] == "claimable" and row["pending_messages"] == []
        _assert_the_copies_hold_no_input(w)
        [user_input] = w.records("user_input")
        assert user_input["payload"][CANVAS_EVENT_PAYLOAD_KEY]["name"] == "submit"
        assert [e["id"] for e in user_input["payload"][CANVAS_DIGEST_PAYLOAD_KEY]["events"]] == ["e1"]
        [own] = w.own_input_texts()
        assert SECRET_NOTE in own and f'value="{CHOSEN}"' in own, "the session's own input text carries what the user entered"
        assert SECRET_NOTE in user_input["payload"]["text"]


async def test_queued(sp, reader) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        w = _World(sp)
        await w.seed(turn_status="running", status=SessionStatus.RUNNING)
        assert await w.interact(EMIT) == DELIVERY_QUEUED

        row = await _assert_the_shared_reads_hold_no_input(reader, w)
        [queued] = row["pending_messages"]
        assert queued["attribution"][CANVAS_EVENT_PAYLOAD_KEY]["name"] == "submit"
        assert SECRET_NOTE in queued["parts"][0]["text"], "the queued text is the block: the session's own next input"
        _assert_the_copies_hold_no_input(w)

        await w.end_turn()
        assert await realize_next_pending(storage_provider=sp, workspace_id=WORKSPACE, session_id=SESSION, wake_deps=w.deps()) is True

        row = await _assert_the_shared_reads_hold_no_input(reader, w)
        assert row["pending_messages"] == [] and row["turn_status"] == "claimable"
        _assert_the_copies_hold_no_input(w)
        [own] = w.own_input_texts()
        assert SECRET_NOTE in own


async def test_wait(sp, reader) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        w = _World(sp)
        await w.seed()
        provider = build_canvas_toolset(storage_provider=sp)
        ctx = ToolContext(tool_call_id="call-1", session_id=SESSION, workspace_id=WORKSPACE, initiated_by=ADMIN_REF)
        with pytest.raises(YieldToWorker) as parked:
            await provider.call(tool_name="canvas_wait", arguments={"ids": ["viz"], "kinds": ["emit"]}, ctx=ctx)
        yielded = parked.value.yielded
        session = await w.sessions.get(SESSION)
        await w.sessions.update(session.model_copy(update={
            "parked_status": "parked", "parked_event_key": yielded.event_key, "parked_at": datetime.now(UTC),
            "parked_state": {"tool_call_id": "call-1", "yielded": {**yielded.to_jsonable(), "tool_name": "canvas_wait"}},
        }))
        bus = FakeBus()
        dispatcher = EventDispatcher(storage_provider=sp, event_bus=bus)
        await dispatcher.drain_once()

        assert await w.interact(EMIT) == DELIVERY_WAIT
        assert await dispatcher.drain_once() == 1
        [(key, envelope)] = bus.published
        assert await flip_sessions_parked_on(key, envelope, session_storage=w.sessions, engine=None) == 1

        row = await _assert_the_shared_reads_hold_no_input(reader, w)
        assert row["parked_state"]["resume_event_payload"]["payload"]["name"] == "submit", "the flip stamped the wake on the row"
        _assert_the_copies_hold_no_input(w)
        [record] = w.records("canvas_event")
        assert record["payload"]["name"] == "submit" and w.records("user_input") == []


async def test_recorded(sp, reader) -> None:
    async with asyncio.timeout(BODY_TIMEOUT_S):
        w = _World(sp)
        await w.seed()
        assert await w.interact(CHANGE) == DELIVERY_RECORDED

        await _assert_the_shared_reads_hold_no_input(reader, w)
        _assert_the_copies_hold_no_input(w)
        [record] = w.records("canvas_event")
        assert record["payload"]["element"] == "genre"

        await wake_session(workspace_id=WORKSPACE, session_id=SESSION, instruction="go on", deps=w.deps(), human_intent=True)

        row = await _assert_the_shared_reads_hold_no_input(reader, w)
        assert row["turn_status"] == "claimable"
        _assert_the_copies_hold_no_input(w)
        [user_input] = w.records("user_input")
        assert [e["id"] for e in user_input["payload"][CANVAS_DIGEST_PAYLOAD_KEY]["events"]] == ["e1"]
        [own] = w.own_input_texts()
        assert f'e1 genre change value="{CHOSEN}"' in own, "the digest in the session's own input carries the value"
