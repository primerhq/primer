"""E2E: §3 NullChannelAdapter in-process tool_approval branch journey.

Sibling to T0856 (which pinned the ``ask_user`` branch end-to-end).
This test pins the OTHER half of the channels contract:

  * Outbound — ``ChannelDispatcher.dispatch_prompt`` with
    ``PromptEnvelope.kind="tool_approval"`` fans out to the channel the
    workspace is bound to (via its ``channel_association`` link) and
    carries the ``tool_approval`` kind through to the adapter.
  * Inbound — ``ChannelInbox.handle_response`` with
    ``ResponseEnvelope.kind="tool_approval"`` composes the event_key
    ``tool_approval:{sid}:{tcid}`` and publishes a payload carrying
    BOTH ``decision`` and ``reason`` (not ``response``, which is the
    ask_user shape).
  * Negative — ``ChannelInbox.handle_response`` on an unknown kind
    raises ``BadRequestError`` (so the surface can't silently swallow
    a typo'd envelope kind).

Subsystems exercised in one test:

  1. Registry + factory wiring: ``register_adapter_factory(SLACK, ...)``
     installs a captured NullChannelAdapter; the dispatcher's lazy
     adapter-build path resolves to it for the bound workspace.
  2. ChannelProvider + Channel + a Workspace row bound to the channel
     via its ``channel_association`` link, all created via in-process
     storage.
  3. ChannelDispatcher resolves the channel from the workspace's
     channel_association and posts the tool_approval envelope to it.
  4. ChannelInbox builds the correct event_key
     (``tool_approval:{sid}:{tcid}``) and publishes
     ``{decision, reason}`` onto the bus.
  5. A pre-subscribed bus listener receives the published event
     end-to-end with the correct event_key + payload.
  6. ChannelInbox.handle_response rejects unknown envelope kinds with
     BadRequestError — the negative path is part of the contract, not
     a swallowed warning.

Covers backlog item T0857. No HTTP — pure in-process orchestration
of the channels subsystem. No LLM, no real network, no Postgres.

NOTE: the old per-flag routing (forward_ask_user / forward_tool_approval
on the standalone association model) was removed with that model; a
workspace now binds to at most one channel via its channel_association
field and every gate kind forwards to it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.channel.adapter import (
    ChannelAdapter,
    PromptEnvelope,
    ResponseEnvelope,
)
from primer.channel.factory import (
    clear_factories_for_tests,
    register_adapter_factory,
)
from primer.channel.null_adapter import NullChannelAdapter
from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    SlackChannelProviderConfig,
)
from primer.model.except_ import BadRequestError
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.model.scheduler import (
    InMemorySchedulerConfig,
    RuntimeMode,
    SchedulerProviderConfig,
    SchedulerProviderType,
)
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
)


# ===========================================================================
# T0857 — In-process NullChannelAdapter tool_approval branch journey
# ===========================================================================


@pytest.mark.asyncio
async def test_t0857_null_channel_adapter_tool_approval_journey(
    tmp_path,
) -> None:
    """T0857 — Channels tool_approval branch end-to-end.

    Steps:

      1. Register a capture-aware factory for ChannelProviderType.SLACK
         that returns a NullChannelAdapter.
      2. Build app with SQLite + in-memory scheduler. Enter lifespan.
      3. Seed ChannelProvider (slack) + Channel + a Workspace bound to
         the channel via its channel_association link.
      4. Dispatch a PromptEnvelope(kind="tool_approval", ...). The
         dispatcher resolves the workspace's channel and posts to
         EXACTLY ONE adapter, carrying the tool_approval kind through.
      5. Subscribe to the event bus. Call inbox.handle_response with a
         ResponseEnvelope(kind="tool_approval", decision="approved",
         reason="looks fine"). Assert the event_key is
         ``tool_approval:{sid}:{tcid}`` and payload contains BOTH
         decision="approved" AND reason="looks fine".
      6. Call inbox.handle_response with an unknown kind ("garbage")
         and assert BadRequestError is raised — the contract refuses
         to silently no-op on a typo'd envelope.

    Pinned invariants:
      * A tool_approval envelope reaches the channel the workspace is
        bound to, carrying kind="tool_approval".
      * Inbox event_key for tool_approval is exactly
        ``tool_approval:{sid}:{tcid}`` (NOT ``ask_user:{...}``).
      * Inbox payload for tool_approval carries decision AND reason
        (NOT response, which is the ask_user shape).
      * Unknown ResponseEnvelope kind → BadRequestError, never a
        silently-swallowed warning.
    """
    # ----- 1. Factory registration ----------------------------------
    captured: list[NullChannelAdapter] = []

    async def _slack_factory(
        provider: ChannelProvider,
        channel: Channel,
        inbox: Any,
        **_kw,
    ) -> ChannelAdapter:
        adapter = NullChannelAdapter()
        captured.append(adapter)
        return adapter

    clear_factories_for_tests()
    register_adapter_factory(ChannelProviderType.SLACK, _slack_factory)
    try:
        # ----- 2. In-process app + lifespan -------------------------
        db_path = tmp_path / "t0857.sqlite"
        cfg = AppConfig(
            runtime_mode=RuntimeMode.API,
            db=StorageProviderConfig(
                provider=StorageProviderType.SQLITE,
                config=SqliteConfig(path=db_path),
            ),
            scheduler=SchedulerProviderConfig(
                provider=SchedulerProviderType.IN_MEMORY,
                config=InMemorySchedulerConfig(),
            ),
        )
        app = create_app(cfg)

        async with app.router.lifespan_context(app):
            sp = app.state.storage_provider
            dispatcher = app.state.channel_dispatcher
            inbox = app.state.channel_inbox
            event_bus = app.state.event_bus
            assert dispatcher is not None, "ChannelDispatcher not on app.state"
            assert inbox is not None, "ChannelInbox not on app.state"
            assert event_bus is not None, "EventBus not on app.state"

            # ----- 3. Seed entities via in-process storage -----------
            cp_storage = sp.get_storage(ChannelProvider)
            ch_storage = sp.get_storage(Channel)
            ws_storage = sp.get_storage(Workspace)

            cp = ChannelProvider(
                id="cp-t857",
                provider=ChannelProviderType.SLACK,
                config=SlackChannelProviderConfig(
                    app_token="xapp-test-token",
                    bot_token="xoxb-test-token",
                ),
            )
            await cp_storage.create(cp)

            ch = Channel(
                id="ch-t857",
                provider_id="cp-t857",
                provider=ChannelProviderType.SLACK,
                external_id="C0123ABC457",
                label="T0857 test channel",
            )
            await ch_storage.create(ch)

            workspace_id = "ws-t857"
            ws = Workspace(
                id=workspace_id,
                template_id="tpl-t857",
                provider_id="wp-t857",
                created_at=datetime.now(timezone.utc),
                runtime_meta=WorkspaceRuntimeMeta(
                    url="ws://localhost:5959",
                    token="runtime-token",
                ),
                channel_association=WorkspaceChannelLink(channel_id="ch-t857"),
            )
            await ws_storage.create(ws)

            # ----- 4. Dispatch a tool_approval envelope --------------
            prompt_env = PromptEnvelope(
                kind="tool_approval",
                workspace_id=workspace_id,
                session_id="sess-t857",
                tool_call_id="tc-t857",
                prompt="May I run `rm -rf /tmp/x`?",
                response_schema=None,
                choices=None,
                timeout_at_iso=None,
            )
            results = await dispatcher.dispatch_prompt(envelope=prompt_env)

            # ----- 5. Exactly one adapter captured (the channel the
            # workspace is bound to); tool_approval kind carried -----
            assert len(captured) == 1, (
                f"expected exactly 1 factory-built adapter (the channel "
                f"the workspace is bound to); got {len(captured)}"
            )
            adapter = captured[0]
            assert len(adapter.posted) == 1, (
                f"NullChannelAdapter.posted should have exactly 1 "
                f"tool_approval envelope; got {len(adapter.posted)}"
            )
            posted = adapter.posted[0]
            assert posted.kind == "tool_approval"
            assert posted.workspace_id == workspace_id
            assert posted.session_id == "sess-t857"
            assert posted.tool_call_id == "tc-t857"
            assert posted.prompt == "May I run `rm -rf /tmp/x`?"

            # Dispatcher's return value carries the per-adapter dict
            assert len(results) == 1, results
            assert results[0].get("posted") is True, results
            assert results[0].get("kind") == "tool_approval", results

            # ----- 6. Subscribe + handle a tool_approval response ---
            subscription = event_bus.subscribe()
            try:
                resp_env = ResponseEnvelope(
                    kind="tool_approval",
                    workspace_id=workspace_id,
                    session_id="sess-t857",
                    tool_call_id="tc-t857",
                    response=None,
                    decision="approved",
                    reason="looks fine",
                )
                await inbox.handle_response(resp_env)

                async def _drain_one():
                    async for ev in subscription:
                        return ev
                    return None

                event = await asyncio.wait_for(_drain_one(), timeout=2.0)
                assert event is not None, (
                    "ChannelInbox.handle_response should have "
                    "published a tool_approval event onto the bus, "
                    "but the subscription received nothing within 2s"
                )
                expected_key = "tool_approval:sess-t857:tc-t857"
                assert event.event_key == expected_key, (
                    f"event_key mismatch: expected {expected_key!r}, "
                    f"got {event.event_key!r} — the tool_approval "
                    f"branch may be incorrectly routing through the "
                    f"ask_user key namespace"
                )
                # Payload shape is the approval shape — decision +
                # reason, NOT response (which would be the ask_user
                # payload shape).
                assert event.payload.get("decision") == "approved", (
                    f"tool_approval payload missing decision='approved': "
                    f"{event.payload!r}"
                )
                assert event.payload.get("reason") == "looks fine", (
                    f"tool_approval payload missing reason='looks fine': "
                    f"{event.payload!r}"
                )
                assert "response" not in event.payload, (
                    f"tool_approval payload must NOT carry the "
                    f"ask_user-shaped `response` field: "
                    f"{event.payload!r}"
                )
            finally:
                await subscription.aclose()

            # ----- 7. Unknown envelope kind raises BadRequestError --
            unknown_env = ResponseEnvelope(
                kind="garbage_kind",
                workspace_id=workspace_id,
                session_id="sess-t857",
                tool_call_id="tc-t857",
                response=None,
                decision=None,
                reason=None,
            )
            with pytest.raises(BadRequestError) as excinfo:
                await inbox.handle_response(unknown_env)
            # The contract: error message names the offending kind so
            # operators can find the source of the typo'd envelope.
            assert "garbage_kind" in str(excinfo.value), (
                f"BadRequestError should name the offending kind; "
                f"got: {excinfo.value!r}"
            )
    finally:
        # Reset factory state so other tests in the same iteration
        # don't see our SLACK registration.
        clear_factories_for_tests()
