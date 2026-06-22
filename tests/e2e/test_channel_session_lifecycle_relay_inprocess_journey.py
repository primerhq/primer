"""E2E: session final-result relay to the binding, with LAZY thread creation.

Drives ``run_one_session_turn`` offline against a stub executor that streams a
short assistant message and ends ``completed``. The session carries a
session-scoped reply binding in its ``metadata`` (the shape the inbound router
stamps when a channel event spawns a session), resolved through the real
:func:`primer.channel.reply_binding.resolve_reply_binding` /
:meth:`ChannelRegistry.for_session` path to a captured
:class:`NullChannelAdapter`.

Asserts the adapter received EXACTLY ONE post -- the final result (the streamed
assistant text) on clean completion. There is deliberately NO start ack: a
per-session thread is created LAZILY on the first real outbound post, so an
unconditional turn-0 ack would open an empty thread for every session in a
binding-bearing workspace.

Pure in-process orchestration: no HTTP, no LLM, no Postgres, no real network.
Gated behind ``PRIMER_RUN_E2E`` like the other in-process journeys.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.dispatcher import ChannelDispatcher
from primer.channel.null_adapter import NullChannelAdapter
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Done, TextDelta
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn


pytestmark = pytest.mark.skipif(
    not os.getenv("PRIMER_RUN_E2E"),
    reason="in-process e2e journey; set PRIMER_RUN_E2E=1 to run",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_lease(session_id: str) -> Lease:
    now = _now()
    return Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
        claimed_at=now, expires_at=now, attempt_count=1, last_error=None,
    )


class _FakeWorkspaceIO:
    """Captures messages.jsonl lines and replays them for the final-text scan."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = defaultdict(bytes)

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self._data[(session_id, "messages.jsonl")] += line

    def read_lines(
        self, session_id: str, filename: str = "messages.jsonl",
    ) -> list[str]:
        raw = self._data.get((session_id, filename), b"")
        return [ln for ln in raw.decode().splitlines() if ln.strip()]


class _StreamingExecutor:
    """Streams a short assistant message then ends with a clean stop."""

    last_done_reason = "stop"

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text="All ", index=0)
        yield TextDelta(text="finished.", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


class _SilentExecutor:
    """Completes cleanly without emitting any assistant text.

    Models a background/graph/test session that runs in a binding-bearing
    workspace but produces nothing the operator should see: no gate, no
    inform, no assistant output. Such a session must open NO thread.
    """

    last_done_reason = "stop"

    async def invoke(self, messages: list[Any], **kwargs: Any):
        if False:  # pragma: no cover - generator with no yields
            yield None
        yield Done(stop_reason="stop", raw_reason="stop")


class _Registry:
    """Stub registry borrowing the real ``for_session`` resolution path."""

    def __init__(self) -> None:
        self._storage_provider = None
        self.adapters: dict[str, NullChannelAdapter] = {}

    def bind(self, storage_provider) -> None:
        self._storage_provider = storage_provider

    async def get_adapter(self, channel_id: str) -> NullChannelAdapter:
        adapter = self.adapters.get(channel_id)
        if adapter is None:
            adapter = NullChannelAdapter()
            await adapter.initialize()
            self.adapters[channel_id] = adapter
        return adapter

    async def for_session(self, session):
        from primer.api.registries.channel_registry import ChannelRegistry

        return await ChannelRegistry.for_session(self, session)


@pytest.mark.asyncio
async def test_channel_session_lifecycle_relay_journey() -> None:
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()

    # Seed the workspace (loaded for attribution; no workspace-standing
    # binding so the session-scoped binding is the sole resolution path).
    ws = Workspace(
        id="ws-lr",
        template_id="tpl-lr",
        provider_id="wp-lr",
        created_at=_now(),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://localhost:5959",
            token=SecretStr("runtime-token"),
        ),
    )
    await sp.get_storage(Workspace).create(ws)

    # The session carries the session-scoped reply binding the inbound router
    # would stamp on a channel-triggered session.
    session = WorkspaceSession(
        id="s-lr",
        workspace_id="ws-lr",
        binding=AgentSessionBinding(agent_id="ag-lr"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
        turn_no=0,
        metadata={SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-sess"}},
    )
    await sp.get_storage(WorkspaceSession).create(session)

    registry = _Registry()
    registry.bind(sp)
    dispatcher = ChannelDispatcher(registry=registry)

    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        async def _build_executor(_session: WorkspaceSession):
            return _StreamingExecutor()

        deps = SessionDispatchDeps(
            storage_provider=sp,
            workspace_io=_FakeWorkspaceIO(),
            event_bus=bus,
            build_executor=_build_executor,
            channel_dispatcher=dispatcher,
        )

        outcome = await run_one_session_turn(_make_lease(session.id), deps)
    finally:
        await bus.aclose()

    assert outcome.success is True
    assert outcome.park is None

    adapter = registry.adapters["ch-sess"]
    posted = adapter.posted
    # Exactly ONE post: the final result. No turn-0 start ack -- the thread is
    # created lazily by this first (and only) real outbound post.
    assert len(posted) == 1, [p.prompt for p in posted]
    assert posted[-1].kind == "inform"
    assert posted[-1].prompt == "All finished."


@pytest.mark.asyncio
async def test_silent_session_in_bound_workspace_opens_no_thread() -> None:
    """Regression: a background session that never posts opens NO thread.

    The workspace carries a workspace-STANDING reply binding (the exact bug
    scenario: every session in the workspace resolves it), and the session has
    no session-scoped binding -- it is an ordinary background/graph/test
    session. The executor completes cleanly without emitting any assistant
    text. With lazy thread creation, ``post_prompt`` is never called and no
    adapter is even requested, so no empty thread is opened.
    """
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()

    # Workspace-standing binding: shared by every session in this workspace.
    ws = Workspace(
        id="ws-silent",
        template_id="tpl-s",
        provider_id="wp-s",
        created_at=_now(),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://localhost:5959",
            token=SecretStr("runtime-token"),
        ),
        reply_binding=WorkspaceChannelLink(channel_id="ch-standing"),
    )
    await sp.get_storage(Workspace).create(ws)

    # An ordinary background session: NO session-scoped reply binding.
    session = WorkspaceSession(
        id="s-silent",
        workspace_id="ws-silent",
        binding=AgentSessionBinding(agent_id="ag-s"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
        turn_no=0,
        metadata={},
    )
    await sp.get_storage(WorkspaceSession).create(session)

    registry = _Registry()
    registry.bind(sp)
    dispatcher = ChannelDispatcher(registry=registry)

    bus = InMemoryEventBus()
    await bus.initialize()
    try:
        async def _build_executor(_session: WorkspaceSession):
            return _SilentExecutor()

        deps = SessionDispatchDeps(
            storage_provider=sp,
            workspace_io=_FakeWorkspaceIO(),
            event_bus=bus,
            build_executor=_build_executor,
            channel_dispatcher=dispatcher,
        )

        outcome = await run_one_session_turn(_make_lease(session.id), deps)
    finally:
        await bus.aclose()

    assert outcome.success is True
    assert outcome.park is None

    # No adapter was ever requested -> no thread get-or-created -> no empty
    # thread. The whole point of the lazy fix.
    assert registry.adapters == {}
