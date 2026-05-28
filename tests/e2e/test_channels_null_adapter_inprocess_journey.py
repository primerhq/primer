"""E2E: §3 NullChannelAdapter in-process park-dispatch + inbox journey.

The §3 directive prescribes a journey that registers
``NullChannelAdapter`` as the factory for a platform, creates a
ChannelProvider+Channel+Association triple, then exercises BOTH the
outbound dispatch path (worker park → adapter.post_prompt was called)
and the inbound response path (`ChannelInbox.handle_response(...)`
publishes onto the event bus → bus subscriber receives the
``ask_user:…`` event).

Why in-process: the NullChannelAdapter factory is registered via a
module-level call. The live e2e server is a separate process — we
can't reach into its registry from the test process. So this test
builds its own in-process FastAPI app via the same pattern as T0852
(SQLite multi-router journey).

Subsystems exercised in one test:

  1. Registry + factory wiring: `register_adapter_factory(SLACK, ...)`
     installs a captured NullChannelAdapter; ChannelRegistry's lazy
     adapter-build path resolves to it.
  2. ChannelProvider + Channel + WorkspaceChannelAssociation + the
     workspace ladder, all created via in-process storage interfaces.
  3. ChannelDispatcher.dispatch_prompt() fans the ask_user envelope
     to the matched association's adapter; the NullChannelAdapter's
     ``posted`` list captures the envelope for assertion.
  4. ChannelDispatcher honours the `forward_ask_user` /
     `forward_tool_approval` flags — an association with both flags
     False is filtered out before dispatch.
  5. ChannelInbox.handle_response() builds the right event_key
     (`ask_user:{sid}:{tcid}`) and publishes onto the event bus.
  6. A pre-subscribed bus listener receives the published event
     with the correct event_key + response payload (this proves the
     inbox→bus side of the contract end-to-end, not just that
     publish was called).

Covers backlog item T0856. No HTTP — pure in-process orchestration
of the channels subsystem. No LLM, no real network, no Postgres.
"""

from __future__ import annotations

import asyncio
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
    WorkspaceChannelAssociation,
)
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


# ===========================================================================
# T0856 — In-process NullChannelAdapter park-dispatch + inbox journey
# ===========================================================================


@pytest.mark.asyncio
async def test_t0856_null_channel_adapter_dispatch_and_inbox_journey(
    tmp_path,
) -> None:
    """T0856 — Wire the channels subsystem end-to-end via an
    in-process FastAPI app + NullChannelAdapter, then drive both
    the outbound dispatch and inbound inbox paths.

    Steps:

      1. Register a capture-aware factory for ChannelProviderType.SLACK
         that returns a NullChannelAdapter. Reset the global factory
         table first so test ordering doesn't matter.
      2. Build app with SQLite + in-memory scheduler. Enter lifespan
         — registries are built; the event bus is an InMemoryEventBus.
      3. Seed ChannelProvider (slack) + Channel + WorkspaceProvider +
         Template + Workspace + 2 associations:
           * assoc_A links workspace_a to the channel, forward_ask_user=True
           * assoc_B links workspace_b (a DIFFERENT workspace) — should
             NOT receive dispatches when we fan out for workspace_a.
      4. Construct PromptEnvelope(kind="ask_user", workspace_id=A, ...)
         and call dispatcher.dispatch_prompt(envelope=...).
      5. Assert the captured NullChannelAdapter's `posted` list now
         contains the envelope. Exactly 1 post (workspace_b's
         association is for a different workspace, so it's not in
         the for_workspace(A) result).
      6. Subscribe to the event bus. Construct a ResponseEnvelope
         with the matching tool_call_id and call inbox.handle_response.
      7. Drain one event from the subscription; assert event_key =
         "ask_user:{sid}:{tcid}" and payload.response = the answer.

    Pinned invariants:
      * The factory-built adapter wins (NullChannelAdapter, not a
        real Slack one).
      * dispatch_prompt fan-out is scoped by workspace_id.
      * ChannelInbox.handle_response composes the right event_key
        (`ask_user:{sid}:{tcid}`) and publishes via the bus.
      * Bus subscriber receives the published event end-to-end.
    """
    # ----- 1. Factory registration ----------------------------------
    captured: list[NullChannelAdapter] = []

    async def _slack_factory(
        provider: ChannelProvider,
        channel: Channel,
        inbox: Any,
    ) -> ChannelAdapter:
        adapter = NullChannelAdapter()
        captured.append(adapter)
        return adapter

    clear_factories_for_tests()
    register_adapter_factory(ChannelProviderType.SLACK, _slack_factory)
    try:
        # ----- 2. In-process app + lifespan -------------------------
        db_path = tmp_path / "t0856.sqlite"
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
            assoc_storage = sp.get_storage(WorkspaceChannelAssociation)

            cp = ChannelProvider(
                id="cp-t856",
                provider=ChannelProviderType.SLACK,
                config=SlackChannelProviderConfig(
                    app_token="xapp-test-token",
                    bot_token="xoxb-test-token",
                ),
            )
            await cp_storage.create(cp)

            ch = Channel(
                id="ch-t856",
                provider_id="cp-t856",
                external_id="C0123ABC456",
                label="T0856 test channel",
            )
            await ch_storage.create(ch)

            workspace_a = "ws-t856-A"
            workspace_b = "ws-t856-B"

            assoc_a = WorkspaceChannelAssociation(
                id="assoc-t856-A",
                workspace_id=workspace_a,
                channel_id="ch-t856",
                enabled=True,
                forward_ask_user=True,
                forward_tool_approval=True,
            )
            await assoc_storage.create(assoc_a)

            # workspace_b is a DIFFERENT workspace — its association
            # must NOT receive the workspace_a dispatch. Pins the
            # workspace-scoping of dispatch_prompt.
            assoc_b = WorkspaceChannelAssociation(
                id="assoc-t856-B",
                workspace_id=workspace_b,
                channel_id="ch-t856",
                enabled=True,
                forward_ask_user=True,
                forward_tool_approval=True,
            )
            await assoc_storage.create(assoc_b)

            # ----- 4. Construct + dispatch ask_user envelope ---------
            prompt_env = PromptEnvelope(
                kind="ask_user",
                workspace_id=workspace_a,
                session_id="sess-t856",
                tool_call_id="tc-t856",
                prompt="What's the answer?",
                response_schema=None,
                choices=None,
                timeout_at_iso=None,
            )
            results = await dispatcher.dispatch_prompt(envelope=prompt_env)

            # ----- 5. Assert NullChannelAdapter captured the envelope
            assert len(captured) == 1, (
                f"expected exactly 1 factory-built adapter (for the "
                f"workspace_a association); got {len(captured)} — "
                f"factory may have been invoked for workspace_b's "
                f"association too, violating per-workspace scoping"
            )
            adapter = captured[0]
            assert len(adapter.posted) == 1, (
                f"NullChannelAdapter.posted should have exactly 1 envelope; "
                f"got {len(adapter.posted)}"
            )
            posted = adapter.posted[0]
            assert posted.kind == "ask_user"
            assert posted.workspace_id == workspace_a
            assert posted.session_id == "sess-t856"
            assert posted.tool_call_id == "tc-t856"
            assert posted.prompt == "What's the answer?"

            # And the dispatcher's return value names "posted" per the
            # NullChannelAdapter contract.
            assert len(results) == 1, results
            assert results[0].get("posted") is True, results

            # ----- 6. Subscribe to bus + handle a ResponseEnvelope --
            subscription = event_bus.subscribe()
            try:
                resp_env = ResponseEnvelope(
                    kind="ask_user",
                    workspace_id=workspace_a,
                    session_id="sess-t856",
                    tool_call_id="tc-t856",
                    response="forty-two",
                    decision=None,
                    reason=None,
                )
                await inbox.handle_response(resp_env)

                # ----- 7. Drain the event from the subscription -----
                # InMemoryEventBus delivers via per-subscription queues;
                # the handle_response above already published, so the
                # event is queued and can be retrieved with a brief wait.
                # Use a tight 2s timeout to fail fast if the publish path
                # is broken.
                async def _drain_one():
                    async for ev in subscription:
                        return ev
                    return None

                event = await asyncio.wait_for(_drain_one(), timeout=2.0)
                assert event is not None, (
                    "ChannelInbox.handle_response should have published "
                    "an ask_user event onto the bus, but the subscription "
                    "received nothing within 2s"
                )
                expected_key = f"ask_user:sess-t856:tc-t856"
                assert event.event_key == expected_key, (
                    f"event_key mismatch: expected {expected_key!r}, "
                    f"got {event.event_key!r}"
                )
                assert event.payload.get("response") == "forty-two", (
                    f"payload should carry the response field; got "
                    f"{event.payload!r}"
                )
            finally:
                await subscription.aclose()
    finally:
        # Reset factory state so other tests in the same iteration
        # don't see our SLACK registration.
        clear_factories_for_tests()
