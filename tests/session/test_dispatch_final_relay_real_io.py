"""The final-result relay through the PRODUCTION read path: the pool's ``_WorkspaceIOShim`` over a real workspace.

``read_session_final_text`` reads ``messages.jsonl`` through whatever the dispatch hands it. The pool's only
``SessionDispatchDeps`` passes ``workspace_io=_WorkspaceIOShim``, which has a write side (``append_message_line``) and
no read surface the relay can use (no ``read_lines``, no ``read_file``), so the reader returned ``None`` and the relay
skipped the post without a word: every reply-bound session (Discord/Slack-triggered, thread-mapped, workspace
reply binding) never got its answer. Every relay test used a fake IO that HAS ``read_lines``, which no production
object has, so none of them could see it. These run the real shim over a real ``LocalWorkspace``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.int.claim import ClaimKind, Lease
from primer.model.chat import Done, TextDelta
from primer.model.envelope import RELAY_EVERY_TURN_KEY
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.worker.io_shim import _WorkspaceIOShim
from tests._support.off_golden import open_session
from tests.conftest import _FakeStorageProvider

BINDING = {"channel_id": "ch-1", "anchor": "thr-1", "quiet": False}
WORKSPACE_ID = "w1"


class _Registry:
    """The workspace registry the pool holds: ``get_workspace`` resolves the real workspace by id."""

    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self._workspace if workspace_id == WORKSPACE_ID else None


class _Executor:
    def __init__(self, text: str, stop_reason: str = "stop") -> None:
        self._text, self._stop_reason = text, stop_reason
        self.last_done_reason = stop_reason

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text=self._text, index=0)
        yield Done(stop_reason=self._stop_reason, raw_reason=self._stop_reason)


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def dispatch_prompt(self, *, envelope, session=None):
        self.texts.append(envelope.prompt)
        return [{"ok": True}]


def _lease(session_id: str) -> Lease:
    now = datetime.now(UTC)
    return Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1",
        claimed_at=now, expires_at=now, attempt_count=1, last_error=None,
    )


class _RaisingRegistry:
    async def get_workspace(self, workspace_id: str):
        raise RuntimeError("registry unavailable")


async def _run_with_the_pools_io(tmp_path, *, metadata: dict, text: str = "here is the answer", stop_reason: str = "stop",
                                 registry_in_deps: bool = True, deps_registry=None):
    """One turn through ``run_one_session_turn`` with the REAL shim over a REAL workspace, as ``WorkerPool`` builds it."""
    backend, workspace, session = await open_session(tmp_path)
    try:
        registry = _Registry(workspace)
        shim = _WorkspaceIOShim(registry)
        shim.register_session(session.session_id, WORKSPACE_ID)
        sp = _FakeStorageProvider()
        await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
            id=session.session_id, workspace_id=WORKSPACE_ID, binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.RUNNING, created_at=datetime.now(UTC), turn_status="running", metadata=metadata,
        ))
        bus = InMemoryEventBus()
        await bus.initialize()
        dispatcher = _RecordingDispatcher()

        async def build(_session):
            return _Executor(text, stop_reason)

        await run_one_session_turn(_lease(session.session_id), SessionDispatchDeps(
            storage_provider=sp, workspace_io=shim, event_bus=bus, build_executor=build,
            channel_dispatcher=dispatcher,
            workspace_registry=deps_registry if deps_registry is not None else (registry if registry_in_deps else None),
        ))
        await bus.aclose()
        return dispatcher
    finally:
        await session.aclose()
        await backend.aclose()


class TestTheRelayReadsThroughTheWorkspaceTheWayProductionIsWired:
    async def test_a_reply_bound_session_that_completes_posts_its_final_answer(self, tmp_path) -> None:
        dispatcher = await _run_with_the_pools_io(tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING})
        assert dispatcher.texts == ["here is the answer"]

    async def test_a_thread_mapped_session_relays_every_turn_through_the_same_path(self, tmp_path) -> None:
        """``relay_every_turn``: a turn that leaves the session WAITING still posts its answer (max_tokens here)."""
        dispatcher = await _run_with_the_pools_io(
            tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING, RELAY_EVERY_TURN_KEY: True}, stop_reason="max_tokens",
        )
        assert dispatcher.texts == ["here is the answer"]

    async def test_a_session_without_a_reply_binding_stays_silent(self, tmp_path) -> None:
        dispatcher = await _run_with_the_pools_io(tmp_path, metadata={})
        assert dispatcher.texts == []

    async def test_when_the_final_text_cannot_be_read_a_reply_bound_session_says_so_in_the_log(
        self, tmp_path, caplog,
    ) -> None:
        """Without a registry the dispatch has only the shim to read through: it cannot, and a reply-bound session
        that silently posts nothing is what hid this for months."""
        with caplog.at_level(logging.WARNING):
            dispatcher = await _run_with_the_pools_io(
                tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING}, registry_in_deps=False,
            )
        assert dispatcher.texts == []
        assert any("no final text" in r.getMessage() for r in caplog.records), "the silent skip is now a warning"

    async def test_a_quiet_binding_does_not_warn_about_a_text_it_would_not_post_anyway(self, tmp_path, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            dispatcher = await _run_with_the_pools_io(
                tmp_path, metadata={SESSION_REPLY_BINDING_KEY: {**BINDING, "quiet": True}}, registry_in_deps=False,
            )
        assert dispatcher.texts == []
        assert not any("no final text" in r.getMessage() for r in caplog.records)

    async def test_a_registry_that_raises_does_not_block_the_release(self, tmp_path, caplog) -> None:
        """The relay degrades (it falls back to the shim, which cannot read) and says so; the turn still releases."""
        with caplog.at_level(logging.WARNING):
            dispatcher = await _run_with_the_pools_io(
                tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING}, deps_registry=_RaisingRegistry(),
            )
        assert dispatcher.texts == []
        messages = [r.getMessage() for r in caplog.records]
        assert any("could not be resolved for the final-result relay" in m for m in messages)
        assert any("no final text" in m for m in messages)

