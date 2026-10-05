"""The final-result relay through the PRODUCTION read path: the pool's ``_WorkspaceIOShim`` over a real workspace.

``read_session_final_text`` reads ``messages.jsonl`` through whatever the dispatch hands it. The pool's only
``SessionDispatchDeps`` passes ``workspace_io=_WorkspaceIOShim``, which has a write side (``append_message_line``) and
no read surface the relay can use (no ``read_lines``, no ``read_file``), so the reader returned ``None`` and the relay
skipped the post without a word: every reply-bound session (Discord/Slack-triggered, thread-mapped, workspace
reply binding) never got its answer. Every relay test used a fake IO that HAS ``read_lines``, which no production
object has, so none of them could see it. These run the real shim over a real ``LocalWorkspace``.
"""

from __future__ import annotations

import asyncio
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


class _SpyWorkspace:
    """The real workspace, with every ``read_file`` recorded (and, when asked, never answered)."""

    def __init__(self, workspace, reads: list[str], *, hang: bool = False) -> None:
        self._workspace, self._reads, self._hang = workspace, reads, hang

    def __getattr__(self, name):
        return getattr(self._workspace, name)

    async def read_file(self, path: str):
        self._reads.append(path)
        if self._hang:
            await asyncio.Event().wait()        # a runtime connection that never answers
        return await self._workspace.read_file(path)


class _Registry:
    """The workspace registry the pool holds: ``get_workspace`` resolves the real workspace by id."""

    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str):
        return self._workspace if workspace_id == WORKSPACE_ID else None


class _Run:
    def __init__(self, dispatcher: Any, outcome, reads: list[str]) -> None:
        self.dispatcher, self.outcome, self.reads = dispatcher, outcome, reads

    @property
    def texts(self) -> list[str]:
        return self.dispatcher.texts


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


class _HangingDispatcher:
    """``dispatch_prompt`` never returns (a Discord or Slack request that is never answered); remembers it was started
    and whether it was cancelled out from under the wait."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.started = False
        self.cancelled = False

    async def dispatch_prompt(self, *, envelope, session=None):
        self.started = True
        self.texts.append(envelope.prompt)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


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
                                 registry_in_deps: bool = True, deps_registry=None, hang_reads: bool = False,
                                 dispatcher=None):
    """One turn through ``run_one_session_turn`` with the REAL shim over a REAL workspace, as ``WorkerPool`` builds it."""
    backend, workspace, session = await open_session(tmp_path)
    try:
        reads: list[str] = []
        registry = _Registry(_SpyWorkspace(workspace, reads, hang=hang_reads))
        shim = _WorkspaceIOShim(registry)
        shim.register_session(session.session_id, WORKSPACE_ID)
        sp = _FakeStorageProvider()
        await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
            id=session.session_id, workspace_id=WORKSPACE_ID, binding=AgentSessionBinding(agent_id="ag1"),
            status=SessionStatus.RUNNING, created_at=datetime.now(UTC), turn_status="running", metadata=metadata,
        ))
        bus = InMemoryEventBus()
        await bus.initialize()
        dispatcher = dispatcher if dispatcher is not None else _RecordingDispatcher()

        async def build(_session):
            return _Executor(text, stop_reason)

        outcome = await asyncio.wait_for(run_one_session_turn(_lease(session.session_id), SessionDispatchDeps(
            storage_provider=sp, workspace_io=shim, event_bus=bus, build_executor=build,
            channel_dispatcher=dispatcher,
            workspace_registry=deps_registry if deps_registry is not None else (registry if registry_in_deps else None),
        )), timeout=15)
        await bus.aclose()
        return _Run(dispatcher, outcome, reads)
    finally:
        await session.aclose()
        await backend.aclose()


class TestTheRelayReadsThroughTheWorkspaceTheWayProductionIsWired:
    async def test_a_reply_bound_session_that_completes_posts_its_final_answer(self, tmp_path) -> None:
        run = await _run_with_the_pools_io(tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING})
        assert run.texts == ["here is the answer"]
        assert len(run.reads) == 1 and run.reads[0].endswith("/messages.jsonl"), "the history is read once, through read_file"

    async def test_a_thread_mapped_session_relays_every_turn_through_the_same_path(self, tmp_path) -> None:
        """``relay_every_turn``: a turn that leaves the session WAITING still posts its answer (max_tokens here)."""
        run = await _run_with_the_pools_io(
            tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING, RELAY_EVERY_TURN_KEY: True}, stop_reason="max_tokens",
        )
        assert run.texts == ["here is the answer"]

    async def test_a_session_without_a_reply_binding_stays_silent(self, tmp_path) -> None:
        run = await _run_with_the_pools_io(tmp_path, metadata={})
        assert run.texts == []
        assert run.reads == [], (
            "a session with no channel costs no workspace I/O here: this runs after every clean turn of every session"
        )

    async def test_when_the_final_text_cannot_be_read_a_reply_bound_session_says_so_in_the_log(
        self, tmp_path, caplog,
    ) -> None:
        """Without a registry the dispatch has only the shim to read through: it cannot, and a reply-bound session
        that silently posts nothing is what hid this for months."""
        with caplog.at_level(logging.WARNING):
            run = await _run_with_the_pools_io(
                tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING}, registry_in_deps=False,
            )
        assert run.texts == []
        assert any("no final text" in r.getMessage() for r in caplog.records), "the silent skip is now a warning"

    async def test_a_quiet_binding_does_not_warn_about_a_text_it_would_not_post_anyway(self, tmp_path, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            run = await _run_with_the_pools_io(
                tmp_path, metadata={SESSION_REPLY_BINDING_KEY: {**BINDING, "quiet": True}},
            )
        assert run.texts == [] and run.reads == [], "a quiet binding has nothing to post and reads nothing"
        assert not any("no final text" in r.getMessage() for r in caplog.records)

    async def test_a_registry_that_raises_does_not_block_the_release(self, tmp_path, caplog) -> None:
        """The relay degrades (it falls back to the shim, which cannot read) and says so; the turn still releases."""
        with caplog.at_level(logging.WARNING):
            run = await _run_with_the_pools_io(
                tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING}, deps_registry=_RaisingRegistry(),
            )
        assert run.texts == []
        assert run.outcome.success and run.outcome.drop_lease, "the turn still releases"
        messages = [r.getMessage() for r in caplog.records]
        assert any("could not be resolved for the final-result relay" in m for m in messages)
        assert any("no final text" in m for m in messages)

    async def test_a_read_that_never_returns_cannot_hold_the_lease(self, tmp_path, monkeypatch, caplog) -> None:
        """The read runs before the lease is released, over the workspace's runtime connection, whose client waits for a
        dropped connection forever. It is bounded: the turn releases, and the skipped post is said in the log."""
        import primer.session.dispatch as dispatch

        monkeypatch.setattr(dispatch, "_BEST_EFFORT_IO_TIMEOUT_S", 0.3)
        with caplog.at_level(logging.WARNING):
            run = await _run_with_the_pools_io(tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING}, hang_reads=True)
        assert len(run.reads) == 1, "it did try to read"
        assert run.texts == []
        assert run.outcome.success and run.outcome.drop_lease
        assert any("did not finish within" in r.getMessage() for r in caplog.records)
        assert not any("no final text" in r.getMessage() for r in caplog.records), "one warning, not two"

    async def test_the_binding_is_resolved_once_and_handed_to_the_post(self, tmp_path, monkeypatch) -> None:
        """The dispatch resolves it to decide whether to read at all; the post must not do that database read again."""
        import primer.channel.reply_binding as reply_binding
        import primer.channel.session_relay as session_relay

        calls: list[str] = []
        real = reply_binding.resolve_reply_binding

        async def counting(session, **kwargs):
            calls.append(session.id)
            return await real(session, **kwargs)

        monkeypatch.setattr(reply_binding, "resolve_reply_binding", counting)
        monkeypatch.setattr(session_relay, "resolve_reply_binding", counting)
        run = await _run_with_the_pools_io(tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING})
        assert run.texts == ["here is the answer"]
        assert len(calls) == 1

    async def test_a_channel_post_that_never_returns_cannot_hold_the_lease(self, tmp_path, monkeypatch, caplog) -> None:
        """The post runs before the lease is released too, and ``dispatch_prompt`` fans out to platform APIs (Discord,
        Slack) whose request can hang. It is bounded like the read: the post is cancelled, the turn releases, and the
        log says the message may or may not have reached the channel."""
        import primer.session.dispatch as dispatch

        monkeypatch.setattr(dispatch, "_CHANNEL_POST_TIMEOUT_S", 0.3)
        monkeypatch.setattr(dispatch, "_BEST_EFFORT_IO_TIMEOUT_S", 60.0)   # the OTHER bound: pins which one the post uses
        hanging = _HangingDispatcher()
        loop = asyncio.get_running_loop()
        started = loop.time()
        with caplog.at_level(logging.WARNING):
            run = await _run_with_the_pools_io(
                tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING}, dispatcher=hanging,
            )
        assert loop.time() - started < 5, "the post ran under _CHANNEL_POST_TIMEOUT_S, not under the workspace-read bound"
        assert hanging.started and hanging.texts == ["here is the answer"], "the text was read and the post attempted"
        assert hanging.cancelled, "the bound cancels the post, it does not leave it running behind the release"
        assert run.outcome.success and run.outcome.drop_lease, "the turn still releases"
        warned = [r.getMessage() for r in caplog.records if "posting the final result" in r.getMessage()]
        assert len(warned) == 1 and "did not finish within 0.3s" in warned[0] and "may or may not" in warned[0], warned

    async def test_one_hung_adapter_cancels_the_real_fan_out_and_the_release_goes_ahead(self, tmp_path, monkeypatch) -> None:
        """Through the real ``ChannelDispatcher``: ``asyncio.gather`` over the adapters is cancelled with the post, so an
        adapter that hangs is stopped and not abandoned."""
        import primer.session.dispatch as dispatch
        from primer.channel.dispatcher import ChannelDispatcher

        monkeypatch.setattr(dispatch, "_CHANNEL_POST_TIMEOUT_S", 0.3)
        monkeypatch.setattr(dispatch, "_BEST_EFFORT_IO_TIMEOUT_S", 60.0)   # the OTHER bound: pins which one the post uses
        seen: dict[str, bool] = {"started": False, "cancelled": False}

        class _HungAdapter:
            async def post_prompt(self, envelope):
                seen["started"] = True
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    seen["cancelled"] = True
                    raise

        class _AdapterRegistry:
            async def for_session(self, session):
                return [_HungAdapter()]

        dispatcher = ChannelDispatcher(registry=_AdapterRegistry())
        run = await _run_with_the_pools_io(
            tmp_path, metadata={SESSION_REPLY_BINDING_KEY: BINDING}, dispatcher=dispatcher,
        )
        assert seen == {"started": True, "cancelled": True}
        assert run.outcome.success and run.outcome.drop_lease
