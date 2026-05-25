"""E2E: §3 NullChannelAdapter in-process tool_approval branch journey.

Sibling to T0856 (which pinned the ``ask_user`` branch end-to-end).
This test pins the OTHER half of the channels contract:

  * Outbound — ``ChannelDispatcher.dispatch_prompt`` with
    ``PromptEnvelope.kind="tool_approval"`` fans out only to
    associations with ``forward_tool_approval=True``. An association
    that forwards ask_user but NOT tool_approval is filtered out
    (inverse of T0856's per-flag check).
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
     adapter-build path resolves to it for the matched association.
  2. ChannelProvider + Channel + WorkspaceChannelAssociation × 2 with
     mismatched forward_* flags, all created via in-process storage.
  3. ChannelDispatcher honours BOTH flag bits independently — an
     association with ``forward_ask_user=True / forward_tool_approval=
     False`` is filtered out for a tool_approval dispatch, even though
     it would receive an ask_user dispatch.
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
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from matrix.api.app import create_app
from matrix.api.config import AppConfig
from matrix.channel.adapter import (
    ChannelAdapter,
    PromptEnvelope,
    ResponseEnvelope,
)
from matrix.channel.factory import (
    clear_factories_for_tests,
    register_adapter_factory,
)
from matrix.channel.null_adapter import NullChannelAdapter
from matrix.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    SlackChannelProviderConfig,
    WorkspaceChannelAssociation,
)
from matrix.model.except_ import BadRequestError
from matrix.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from matrix.model.scheduler import (
    InMemorySchedulerConfig,
    RuntimeMode,
    SchedulerProviderConfig,
    SchedulerProviderType,
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
      3. Seed ChannelProvider (slack) + Channel + 2 associations on
         the SAME workspace with mismatched flags:
           * assoc_approve_only: forward_ask_user=False,
             forward_tool_approval=True
           * assoc_ask_only:     forward_ask_user=True,
             forward_tool_approval=False  (inverse — should be filtered
             out for a tool_approval dispatch)
      4. Dispatch a PromptEnvelope(kind="tool_approval", ...). The
         dispatcher must fan out to EXACTLY ONE adapter — the one
         backing assoc_approve_only — pinning the inverse-flag routing.
      5. Subscribe to the event bus. Call inbox.handle_response with a
         ResponseEnvelope(kind="tool_approval", decision="approved",
         reason="looks fine"). Assert the event_key is
         ``tool_approval:{sid}:{tcid}`` and payload contains BOTH
         decision="approved" AND reason="looks fine".
      6. Call inbox.handle_response with an unknown kind ("garbage")
         and assert BadRequestError is raised — the contract refuses
         to silently no-op on a typo'd envelope.

    Pinned invariants:
      * Routing flags are independent: forward_tool_approval=False
        on an association blocks tool_approval dispatch even when
        forward_ask_user=True.
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
            assoc_storage = sp.get_storage(WorkspaceChannelAssociation)

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
                external_id="C0123ABC457",
                label="T0857 test channel",
            )
            await ch_storage.create(ch)

            workspace_id = "ws-t857"

            # assoc_approve_only: only forwards tool_approval. Must
            # receive the tool_approval dispatch below.
            assoc_approve_only = WorkspaceChannelAssociation(
                id="assoc-t857-approve",
                workspace_id=workspace_id,
                channel_id="ch-t857",
                enabled=True,
                forward_ask_user=False,
                forward_tool_approval=True,
            )
            await assoc_storage.create(assoc_approve_only)

            # assoc_ask_only: only forwards ask_user. The dispatcher
            # MUST filter this one out for a tool_approval dispatch
            # even though both associations point at the SAME workspace
            # and SAME channel. This pins independent per-flag routing.
            assoc_ask_only = WorkspaceChannelAssociation(
                id="assoc-t857-ask",
                workspace_id=workspace_id,
                channel_id="ch-t857",
                enabled=True,
                forward_ask_user=True,
                forward_tool_approval=False,
            )
            await assoc_storage.create(assoc_ask_only)

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

            # ----- 5. Exactly one adapter captured (the approve_only
            # one); inverse-flag association was filtered out --------
            assert len(captured) == 1, (
                f"expected exactly 1 factory-built adapter (the "
                f"forward_tool_approval=True association); got "
                f"{len(captured)} — dispatcher may have ignored the "
                f"per-flag routing and fanned out to the inverse "
                f"association too"
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
