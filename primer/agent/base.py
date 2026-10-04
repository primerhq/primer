"""Shared base class for agent executors.

The :class:`_BaseAgentExecutor` is intentionally non-public (leading
underscore in the name); concrete executors are
:class:`primer.agent.AgentExecutor` (chat threads) and
:class:`primer.agent.WorkspaceAgentExecutor` (workspace-backed).

The base class owns:

* The inner LLM loop -- stream events, buffer, dispatch tool calls,
  re-send with tool results, repeat.
* End-of-turn persistence -- materialise the assistant
  :class:`Message` from the streamed events via
  :func:`primer.model.chat.output_to_message`, hand off to the
  subclass's ``_persist_turn`` hook.
* Compaction integration -- call
  :meth:`CompactionStrategy.maybe_compact` before each turn; if it
  fired, hand the compacted history to the subclass's
  ``_replace_compacted_head`` hook.
* Streaming-tap fan-out -- :meth:`subscribe` registers a callback
  that receives every :class:`StreamEvent` concurrently with the
  caller's iterator.
* Hard-overflow recovery -- catch a context-overflow
  :class:`BadRequestError` from the LLM, force-compact, retry once.

Subclasses provide three abstract hooks:

* :meth:`_load_history`
* :meth:`_persist_turn`
* :meth:`_replace_compacted_head`
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from primer.agent.compaction import CompactionStrategy
from primer.agent.compaction_mixin import (
    should_compact as _mixin_should_compact,
)
from primer.agent.events import (
    AgentEventSubscriber,
    Subscription,
    _ExecutorToolResult,
)
from primer.agent.overflow import is_context_overflow, tool_rounds, whole_rounds
from primer.agent.prompt_render import render_system_prompt_or_raw
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import (
    ExtendedEvent,
    Message,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
    output_to_message,
)
from primer.model.except_ import (
    AuthRequiredError,
    BadRequestError,
    PrimerError,
)
from primer.model.graph import build_execution_context


if TYPE_CHECKING:
    from collections.abc import Callable

    from primer.agent.loop import PromptGuard
    from primer.int.artifact_storage import ArtifactStorage
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model_profile import ResolvedModel


logger = logging.getLogger(__name__)


class _BaseAgentExecutor(ABC):
    """Shared LLM loop + compaction + streaming for both executor types."""

    def __init__(
        self,
        *,
        agent: "Agent",
        llm: "LLM",
        llm_model: "ResolvedModel",
        tool_manager: ToolExecutionManager,
        compaction: CompactionStrategy | None = None,
        principal: str | None = None,
        artifact_storage: "ArtifactStorage | None" = None,
        turn_no: int | None = None,
        tool_calls_as_claims_enabled: bool = False,
    ) -> None:
        self._agent = agent
        self._llm = llm
        self._model = llm_model
        self._tool_manager = tool_manager
        self._compaction = compaction or CompactionStrategy()
        self._principal = principal
        # Forwarded to run_agent_turn so artifact-backed parts (image/
        # document attachments) resolve to inline bytes before the LLM
        # sees them. None is a no-op -- see hydrate_prompt_parts.
        self._artifact_storage = artifact_storage
        # 01a0518b: the enclosing session turn's own turn number, set
        # once per executor instance (a fresh executor is built per turn,
        # same lifetime scope as artifact_storage above) and forwarded to
        # run_agent_turn so the tool-dispatch seam can scope any
        # ToolCallTask rows it creates. None is a no-op for callers that
        # haven't opted into tool_calls_as_claims.
        self._turn_no = turn_no
        # 01a0518b: TOP-LEVEL ONLY - deliberately never forwarded into a
        # nested subagent turn (system__invoke_agent). Unlike turn_no
        # above (pure bookkeeping), this flag arms tool_wait parking,
        # which is session-anchored; a nested subagent turn has no
        # session row of its own to park on. Same scope-cut class as
        # artifact_storage's own cut for that surface - see
        # run_agent_turn's docstring for the full reasoning.
        self._tool_calls_as_claims_enabled = tool_calls_as_claims_enabled
        # 01a0518b (seam-split summit): the per-turn closure that resolves a
        # raw provider tool-call id into (scoped_id, record_seq) for the
        # claim-based dispatch seam. Unlike turn_no/artifact_storage/the
        # flag above, this CANNOT be constructor-injected: it closes over
        # the dispatch loop's _CoalesceState, which does not exist yet at
        # executor-build time (primer.session.dispatch builds the executor,
        # THEN creates coalesce_state, THEN starts streaming - see that
        # module's own ordering). None is the correct default for every
        # caller that hasn't opted into tool_calls_as_claims; set via
        # bind_scoped_call_resolver once the caller has a coalesce_state in
        # hand.
        self._resolve_scoped_call: (
            "Callable[[str], tuple[str, int]] | None"
        ) = None
        # Ambient run context exposed to the system prompt as ``ctx``. Base is
        # surface-agnostic -> memory default; subclasses override with the real
        # surface (AgentExecutor -> "chat", WorkspaceAgentExecutor -> "workspace").
        self._execution_context = build_execution_context()
        self._subscribers: dict[str, AgentEventSubscriber] = {}
        self._subscriber_lock = asyncio.Lock()

    # ---- Subclass hooks --------------------------------------------------

    @abstractmethod
    async def _load_history(self) -> list[Message]:
        """Return the prior conversation in chronological order."""

    @abstractmethod
    async def _persist_turn(self, turn_messages: list[Message]) -> None:
        """Append the messages produced during one ``invoke`` call."""

    @abstractmethod
    async def _replace_compacted_head(
        self,
        compacted: list[Message],
        *,
        summary_message: Message | None = None,
        tokens_before: int = 0,
        tokens_after: int = 0,
    ) -> None:
        """Replace the persisted history with the compacted form.

        ``summary_message`` is the assistant-role summary the strategy
        produced (``None`` for pruning-only compaction, where nothing was
        summarised); ``tokens_before`` / ``tokens_after`` are the strategy's
        telemetry. Surfaces that record compaction as an append-only marker
        (the workspace executor) use these to build the marker payload;
        surfaces that rewrite in place (the chat/thread executor) ignore them.
        """

    # ---- Compaction-window hooks (steer-deferral; workspace override) -----

    async def _open_compaction_window(self) -> list[Message] | None:
        """Hook run just before the compaction region. Default: no-op.

        Returns a history snapshot to use for compaction, or ``None`` to let
        the caller fall back to :meth:`_load_history`. The workspace executor
        overrides this to set a per-session ``compacting`` flag AND snapshot
        history under the SAME messages lock, so a steer can never slip
        between the snapshot and the flag (see PR-C). The lock is released
        before the compaction LLM await -- it is NEVER held across it.
        """
        return None

    async def _close_compaction_window(self) -> None:
        """Hook run after the compaction region (in a ``finally``). No-op here.

        The workspace executor overrides this to clear the ``compacting`` flag
        and drain any steers deferred during the window, applying them AFTER
        the compaction marker. Must not raise on the default no-op path.
        """
        return None

    # ---- Public surface --------------------------------------------------

    async def invoke(
        self,
        messages: list[Message],
        *,
        response_format: type[BaseModel] | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run one user-driven turn (or chain of tool turns) against the LLM.

        Yields every :class:`StreamEvent` produced by the LLM in the
        order it arrives, plus synthetic
        :class:`_ExecutorToolResult` events wrapped in
        :class:`ExtendedEvent` for each tool result fed back to the
        model. The same events are fanned out concurrently to every
        registered tap subscriber.

        End-of-turn persistence runs once the LLM produces a
        non-tool-use stop. Streaming chunks are NOT persisted -- only
        the materialised :class:`Message` is.
        """
        # Bracket the pre-turn compaction region with the window hooks so a
        # steer arriving mid-compaction is deferred (workspace surface). The
        # snapshot is taken inside _open_compaction_window under the messages
        # lock (atomic with setting the compacting flag); the base no-op
        # returns None and we fall back to _load_history unchanged. The flag is
        # only live across an ACTUAL LLM await: maybe_compact returns None with
        # no await when compaction does not fire, and pruning-only is
        # synchronous, so steers on non-compaction turns are never deferred.
        snapshot = await self._open_compaction_window()
        try:
            history = (
                snapshot if snapshot is not None else await self._load_history()
            )
            compacted = await self._compaction.maybe_compact(
                agent=self._agent,
                llm=self._llm,
                model=self._model,
                history=history,
                new_messages=messages,
                **self._compaction_tool_kwargs(),
            )
            if compacted is not None:
                await self._replace_compacted_head(
                    compacted.new_messages,
                    summary_message=compacted.summary_message,
                    tokens_before=compacted.estimated_tokens_before,
                    tokens_after=compacted.estimated_tokens_after,
                )
                history = compacted.new_messages
                logger.info(
                    "AgentExecutor: compaction fired",
                    extra={
                        "agent_id": self._agent.id,
                        "before_tokens": compacted.estimated_tokens_before,
                        "after_tokens": compacted.estimated_tokens_after,
                        "pruned": compacted.pruned_tool_outputs,
                        "head_replaced": compacted.head_messages_replaced,
                    },
                )
        finally:
            await self._close_compaction_window()

        try:
            async for ev in self._run_loop(
                history=history,
                new_messages=messages,
                response_format=response_format,
            ):
                yield ev
        except BadRequestError as exc:
            if not is_context_overflow(exc):
                raise
            # The rounds the rejected attempt already ran (its tools HAVE run): the replay
            # continues from them instead of starting the turn again.
            carried = whole_rounds(getattr(exc, "inflight_messages", None) or [])
            logger.warning(
                "AgentExecutor: hard-overflow detected; force-compacting and retrying",
                extra={"agent_id": self._agent.id, "error": str(exc), "carried_messages": len(carried)},
            )
            # Hard-overflow recovery also runs an LLM await (force_compact), so
            # bracket it too; ``history`` is already in hand, so the snapshot
            # from the hook is not needed here (the flag/drain is what matters).
            await self._open_compaction_window()
            try:
                forced = await self._compaction.force_compact(
                    agent=self._agent,
                    llm=self._llm,
                    model=self._model,
                    history=history,
                    **self._compaction_tool_kwargs(),
                )
                await self._replace_compacted_head(
                    forced.new_messages,
                    summary_message=forced.summary_message,
                    tokens_before=forced.estimated_tokens_before,
                    tokens_after=forced.estimated_tokens_after,
                )
                history = forced.new_messages
            finally:
                await self._close_compaction_window()
            try:
                async for ev in self._run_loop(
                    history=history,
                    new_messages=messages,
                    response_format=response_format,
                    inflight=carried,
                    initial_tool_round=tool_rounds(carried),
                    budget=self._compaction.replay_guard(self._model),
                ):
                    yield ev
            except BadRequestError as replay_exc:
                if is_context_overflow(replay_exc):
                    logger.warning(
                        "AgentExecutor: the replay after a forced compaction overflowed too; "
                        "recording the tool rounds the turn ran and failing the turn",
                        extra={"agent_id": self._agent.id, "error": str(replay_exc)},
                    )
                    await self._persist_failed_replay(messages, replay_exc, fallback=carried)
                raise

    def bind_scoped_call_resolver(
        self, resolver: "Callable[[str], tuple[str, int]] | None",
    ) -> None:
        """Bind the per-turn scoped-tool-call-id resolver (01a0518b).

        Post-construction setter, deliberately not a constructor param
        (see ``self._resolve_scoped_call``'s own comment): the caller
        (``primer.session.dispatch``) only has a ``_CoalesceState`` to
        close over AFTER the executor already exists. Callers that never
        opted into ``tool_calls_as_claims`` simply never call this, and
        ``_run_loop`` passes the ``None`` default through unchanged.
        """
        self._resolve_scoped_call = resolver

    def subscribe(self, subscriber: AgentEventSubscriber) -> Subscription:
        """Register a streaming-tap subscriber. Returns the subscription handle."""
        sub_id = f"sub-{uuid.uuid4().hex[:12]}"
        self._subscribers[sub_id] = subscriber
        return Subscription(subscription_id=sub_id, _executor=self)

    async def unsubscribe(self, subscription: Subscription) -> None:
        async with self._subscriber_lock:
            self._subscribers.pop(subscription.subscription_id, None)

    # ---- Inner loop ------------------------------------------------------

    async def _run_loop(
        self,
        *,
        history: list[Message],
        new_messages: list[Message],
        response_format: type[BaseModel] | dict[str, Any] | None,
        inflight: list[Message] | None = None,
        initial_tool_round: int = 0,
        budget: "PromptGuard | None" = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run the turn's LLM/tool loop and persist it.

        ``inflight`` (default none) are the whole tool rounds an earlier attempt of THIS turn
        already ran, carried into a replay after a context overflow: they follow
        ``new_messages`` in the turn's own messages (so they are persisted, and stamped onto a
        park, with the replay's) and are part of the prompt, and ``initial_tool_round`` is how
        many rounds they are, so ``max_tool_turns`` bounds the turn. ``budget`` is an optional
        :class:`PromptGuard` for the loop's calls.
        """
        from primer.agent.loop import run_agent_turn

        carried = list(inflight or [])
        full_turn_messages: list[Message] = [*new_messages, *carried]
        prompt = [*self._build_prompt(history, new_messages), *carried]

        # Shared helper handles the LLM+tool dispatch loop. We tap
        # every event into our subscriber fan-out + caller stream;
        # the helper writes the assistant + tool-result messages
        # directly into ``full_turn_messages`` for end-of-turn
        # persistence below.
        from primer.model.yield_ import ToolWaitPark, YieldToWorker
        try:
            async for event in run_agent_turn(
                agent=self._agent,
                llm=self._llm,
                llm_model=self._model,
                tool_manager=self._tool_manager,
                prompt=prompt,
                response_format=response_format,
                principal=self._principal,
                messages_out=full_turn_messages,
                artifact_storage=self._artifact_storage,
                turn_no=self._turn_no,
                tool_calls_as_claims_enabled=self._tool_calls_as_claims_enabled,
                resolve_scoped_call=self._resolve_scoped_call,
                budget=budget,
                initial_tool_round=initial_tool_round,
            ):
                await self._emit(event)
                yield event
        except YieldToWorker as exc:
            # The tool engine raised mid-turn. ``full_turn_messages``
            # already contains the assistant message that carried the
            # tool_use (loop.py:143 appends it before dispatch), but
            # ``_persist_turn`` hasn't run yet (we only persist on a
            # clean end-of-stream below). Stamp the delta onto the
            # exception so the worker's park hook can preserve it in
            # the parked_state blob — load-bearing for the resume
            # path's [assistant_tool_use, tool_result] history
            # injection.
            #
            # The slice strips ``new_messages`` (which the executor's
            # caller already has) so the stamp is just what this turn
            # accumulated up to the yield point.
            exc.llm_messages = list(full_turn_messages[len(new_messages):])
            raise
        except ToolWaitPark as exc:
            # 01a0518b: same stamp, same reasoning, as the YieldToWorker
            # arm above -- ToolWaitPark is deliberately NOT a
            # YieldToWorker subclass (see its own docstring), so it needs
            # its own explicit arm here too. Minimal raise-time contract
            # (loop.py's _dispatch_as_claims leaves llm_messages unset);
            # this is the "one layer up" that stamps it, mirroring
            # YieldToWorker's precedent exactly.
            exc.llm_messages = list(full_turn_messages[len(new_messages):])
            raise
        except BadRequestError as exc:
            # What this turn ran before the provider rejected a call (whole rounds: an LLM call
            # is what raises, after the previous round's results were appended). The overflow
            # handler in ``invoke`` replays from here instead of from scratch, and persists
            # these if the replay fails too.
            exc.inflight_messages = list(full_turn_messages[len(new_messages):])  # type: ignore[attr-defined]
            raise

        # Persist only when the loop actually produced an assistant
        # message (helper appends it on the first non-tool stop or
        # not at all on empty/error streams).
        produced_assistant = any(
            m.role == "assistant" for m in full_turn_messages[len(new_messages):]
        )
        if produced_assistant:
            await self._persist_turn(full_turn_messages)

    async def _persist_failed_replay(
        self,
        new_messages: list[Message],
        exc: BadRequestError,
        *,
        fallback: list[Message],
    ) -> None:
        """Record the tool rounds a turn ran when its replay after an overflow failed too.

        Those tools HAVE run. Failing without recording them leaves a history the next turn's
        model cannot see them in, so it can run them again: the double execution the replay
        exists to avoid, one turn later. The turn's input follows the same rule as a normal
        end-of-turn persist. Best effort: a failure to write must not mask the overflow.
        """
        inflight = whole_rounds(getattr(exc, "inflight_messages", None) or fallback)
        if not inflight:
            return
        try:
            await self._persist_turn([*new_messages, *inflight])
        except Exception:  # noqa: BLE001 -- the overflow is the error to surface
            logger.exception(
                "AgentExecutor: could not record the tool rounds a failed replay carried",
                extra={"agent_id": self._agent.id},
            )

    # ---- Tool dispatch ---------------------------------------------------

    async def _dispatch_tool_calls(
        self,
        calls: list[ToolCallPart],
    ) -> list[Message]:
        """Dispatch tool calls and return the resulting tool-role messages.

        AuthRequiredError is the only exception that propagates --
        subclasses handle it (chat: terminal stream Error; workspace:
        WAITING transition). All other PrimerErrors are converted to
        ToolResultPart(error=True) by the manager itself.
        """
        result_parts: list[ToolResultPart] = []
        for call in calls:
            try:
                rp = await self._tool_manager.execute(
                    call,
                    principal=self._principal,
                )
            except AuthRequiredError:
                raise
            except PrimerError as exc:  # defence-in-depth.
                rp = ToolResultPart(id=call.id, output=str(exc), error=True)
            result_parts.append(rp)

        if not result_parts:
            return []
        return [Message(role="tool", parts=list(result_parts))]

    # ---- Prompt building -------------------------------------------------

    def _build_prompt(
        self,
        history: list[Message],
        new_messages: list[Message],
    ) -> list[Message]:
        """Assemble the full prompt: system + history + new user input."""
        parts: list[Message] = []
        if self._agent.system_prompt:
            sys_text = render_system_prompt_or_raw(
                self._agent.system_prompt, self._execution_context
            )
            parts.append(
                Message(role="system", parts=[TextPart(text=sys_text)])
            )
        parts.extend(history)
        parts.extend(new_messages)
        return parts

    # ---- Event fan-out ---------------------------------------------------

    async def _emit(self, event: StreamEvent) -> None:
        """Fan ``event`` out to every registered subscriber concurrently."""
        if not self._subscribers:
            return
        async with self._subscriber_lock:
            subs = list(self._subscribers.values())

        async def _safe(sub: AgentEventSubscriber) -> None:
            try:
                await sub.on_event(event)
            except Exception as exc:  # noqa: BLE001 -- subscriber isolation
                logger.warning(
                    "AgentExecutor: subscriber raised; isolating",
                    extra={"error": str(exc)},
                )

        await asyncio.gather(*[_safe(s) for s in subs], return_exceptions=False)

    def _compaction_tool_kwargs(self) -> dict[str, Any]:
        """Extra kwargs for the compaction call when the agent opts into tool
        access during compaction (``compaction_tool_access``); empty otherwise
        so compaction stays a plain text-only summarisation.

        ``event_sink=self._emit`` streams the compaction's tool-call/result
        events to the live tap (the Studio activity rail) without persisting
        them into the agent's history -- only the summary message the strategy
        returns is persisted, via ``_replace_compacted_head``."""
        if not getattr(self._agent, "compaction_tool_access", False):
            return {}
        return {
            "tool_manager": self._tool_manager,
            "event_sink": self._emit,
            "max_tool_turns": self._agent.max_tool_turns,
            "principal": self._principal,
        }


__all__ = ["_BaseAgentExecutor"]
