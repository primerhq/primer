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
from dataclasses import dataclass, field
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
from primer.agent.overflow import completed_rounds, reduce_for_persist, tool_rounds
from primer.agent.prompt_render import render_system_prompt_or_raw
from primer.agent.prune import PruneSet
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import (
    ExtendedEvent,
    Message,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
    _CompactionNote,
    output_to_message,
)
from primer.model.except_ import (
    AuthRequiredError,
    BadRequestError,
    ContextOverflowUnrecoverable,
    PrimerError,
)
from primer.model.graph import build_execution_context


if TYPE_CHECKING:
    from collections.abc import Callable

    from primer.agent.compaction import CompactedTurn
    from primer.model.chat import Tool

    from primer.int.artifact_storage import ArtifactStorage
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model_profile import ResolvedModel


logger = logging.getLogger(__name__)


def _is_context_overflow(exc: BadRequestError) -> bool:
    """Heuristic: is a BadRequestError caused by context overflow?

    The four shipped LLM adapters wrap provider exceptions into
    ``BadRequestError`` without a stable error code for context
    overflow specifically. Match common substrings instead.
    """
    msg = (exc.message or "").lower()
    needles = (
        "context length",
        "context_length",
        "context window",
        "maximum context",
        "context limit",
        "max_tokens",
        "too long",
        "input is too long",
        "tokens exceeds",
        "token limit",
        "prompt is too long",
    )
    return any(n in msg for n in needles)


@dataclass
class _TurnRecord:
    """What one ``invoke`` has produced so far, shared by its attempts.

    ``messages`` is the turn's own messages: its input (``inputs`` leading entries) and then every
    round the model and the tools completed, appended by the loop as they finish. It is the one
    thing the persistence chokepoint reads when a turn ends any way but a park or a normal finish.
    ``durable_rounds`` counts rounds that already reached the history through a compaction marker
    (the overflow recovery folds the rounds so far into the history), so they are not written twice.
    """

    messages: list[Message]
    inputs: int
    persisted: bool = False
    durable_rounds: int = 0
    guard: Any = None
    forced_compaction: bool = False
    replay_attempted: bool = False
    notes: list[ExtendedEvent] = field(default_factory=list)


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
        # Stop signal, bound by the session dispatch once it has created its cancel
        # event (same post-construction shape as the resolver above; see
        # bind_interrupt_event). None for every caller that is never stopped.
        self._interrupt_event: asyncio.Event | None = None
        # True when the LATEST invoke() ended because the Stop signal fired (the loop
        # returns cleanly, so there is no exception to read). Reset at the start of
        # every invoke; dispatch reads it after the event stream ends.
        self.was_interrupted: bool = False
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
        outcome: str = "summarised",
        unreducible: str | None = None,
        trigger_tokens: int | None = None,
        fixed_overhead_tokens: int = 0,
        snapshot: list[Message] | None = None,
    ) -> list[Message] | None:
        """Replace the persisted history with the compacted form.

        ``summary_message`` is the assistant-role summary the strategy
        produced (``None`` for pruning-only compaction, where nothing was
        summarised); ``tokens_before`` / ``tokens_after`` are the strategy's
        telemetry, and ``outcome`` / ``unreducible`` / ``trigger_tokens`` its
        verdict (``insufficient`` when the summary stands and the prompt is
        still over the trigger) and ``fixed_overhead_tokens`` the part of the
        prompt no history can give back that its figures include. ``snapshot`` is the history the compaction
        was computed from: lines written to the persisted history after it
        was taken (a steer, say) are not in ``compacted`` and must survive
        the fold; the hook returns those lines (``None`` or empty when there are
        none) so the caller can hand them to the turn that follows. Surfaces that record compaction as an append-only marker
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

    async def _close_compaction_window(self) -> list[Message]:
        """Hook run after the compaction region (in a ``finally``). No-op here.

        The workspace executor overrides this to clear the ``compacting`` flag
        and drain any steers deferred during the window, applying them AFTER
        the compaction marker; it returns the steers it applied, so the overflow
        replay can be handed them (the history it is built on was fixed before they
        landed). Must not raise on the default no-op path.
        """
        return []

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
        # The part of every prompt no history can give back: the compaction counts it, and the
        # catalogue is handed to the loop so it is fetched once per invoke.
        tools = await self._tool_manager.list_tools(principal=self._principal)
        fixed_overhead = await self.fixed_overhead_tokens(tools)
        snapshot = await self._open_compaction_window()
        notes: list[ExtendedEvent] = []
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
                fixed_overhead=fixed_overhead,
                **self._compaction_tool_kwargs(),
            )
            if compacted is not None:
                await self._replace_compacted_head(
                    compacted.new_messages,
                    summary_message=compacted.summary_message,
                    tokens_before=compacted.estimated_tokens_before,
                    tokens_after=compacted.estimated_tokens_after,
                    outcome=compacted.outcome,
                    unreducible=compacted.unreducible,
                    trigger_tokens=compacted.trigger_tokens,
                    fixed_overhead_tokens=compacted.fixed_overhead_tokens,
                    snapshot=history,
                )
                notes += self._compaction_notes(compacted)
                history = compacted.new_messages
                logger.info(
                    "AgentExecutor: compaction fired",
                    extra={
                        "agent_id": self._agent.id,
                        "outcome": compacted.outcome,
                        "before_tokens": compacted.estimated_tokens_before,
                        "after_tokens": compacted.estimated_tokens_after,
                        "pruned": compacted.pruned_tool_outputs,
                        "head_replaced": compacted.head_messages_replaced,
                    },
                )
        finally:
            await self._close_compaction_window()
        for note in notes:
            await self._emit(note)
            yield note

        record = _TurnRecord(messages=list(messages), inputs=len(messages))
        from primer.model.yield_ import ToolWaitPark, YieldToWorker

        try:
            try:
                async for ev in self._run_loop(
                    history=history,
                    new_messages=messages,
                    response_format=response_format,
                    tools=tools,
                    record=record,
                ):
                    yield ev
            except BadRequestError as exc:
                if not _is_context_overflow(exc):
                    raise
                async for ev in self._recover_from_overflow(
                    exc,
                    history=history,
                    messages=messages,
                    response_format=response_format,
                    tools=tools,
                    fixed_overhead=fixed_overhead,
                    record=record,
                ):
                    yield ev
        except (YieldToWorker, ToolWaitPark):
            # A park is not a failure: the worker keeps the stamped rounds in its parked state.
            raise
        except BaseException as exc:
            # EVERY other way a turn can end (an exception, a failed stream, the generator closed, a
            # hard cancel) leaves the tool rounds it already ran only here. They have run: persisting
            # them is what stops the next turn running them again.
            await self._persist_failed_turn(record, exc)
            raise

    async def _recover_from_overflow(
        self,
        exc: BadRequestError,
        *,
        history: list[Message],
        messages: list[Message],
        response_format: type[BaseModel] | dict[str, Any] | None,
        tools: "list[Tool]",
        fixed_overhead: int,
        record: _TurnRecord,
    ) -> AsyncIterator[StreamEvent]:
        """The turn's own call was rejected as a context overflow: compact, then CONTINUE the turn.

        The tool rounds the rejected attempt completed are folded into the history the compaction
        works on (so a turn that read many files can be shrunk: its early rounds are summarised and
        the opening question and the newest round stay), in the reduced form with ALREADY RAN
        placeholders (the raw output is in the event log; persisting it raw would let the next turn
        overflow on it), and written by the compaction marker. The replay is then a fresh record
        built on the compacted history, with the turn's tool budget carried over, under a prompt
        guard that reduces what it sends. Nothing the turn already ran runs again.
        """
        rounds = completed_rounds(record.messages[record.inputs:])
        del record.messages[record.inputs + len(rounds):]  # a call that never got its result goes
        logger.warning(
            "AgentExecutor: hard-overflow detected; force-compacting and replaying",
            extra={"agent_id": self._agent.id, "error": str(exc), "completed_rounds": tool_rounds(rounds)},
        )
        size = self._compaction._estimate_tokens  # noqa: SLF001 - the strategy's own sizing
        reduced = reduce_for_persist(
            rounds, sticky=PruneSet(), target_tokens=self._compaction.reduced_target(self._model) // 2, size=size,
        )
        notes: list[ExtendedEvent] = []
        carried: list[Message] = []
        drained: list[Message] = []
        # Hard-overflow recovery runs an LLM await (force_compact), so bracket it with the window too.
        await self._open_compaction_window()
        try:
            forced = await self._compaction.force_compact(
                agent=self._agent,
                llm=self._llm,
                model=self._model,
                history=[*history, *messages, *reduced],
                new_messages=[],
                fixed_overhead=fixed_overhead,
                **self._compaction_tool_kwargs(),
            )
            if forced.outcome == "unreducible":
                # Nothing can be shrunk (the fixed part, or the input the model has not answered,
                # already fills the window): replaying the byte-identical prompt would be rejected
                # the same way, so fail now, with a name, instead of spending a model call on it.
                raise ContextOverflowUnrecoverable(
                    f"the model rejected the prompt as too large and compaction cannot shrink it "
                    f"({forced.unreducible}): about {forced.estimated_tokens_after} tokens, of which "
                    f"{forced.fixed_overhead_tokens} are the system prompt and tool schemas, against a "
                    f"context window of {self._model.context_length}",
                    cause=exc,
                    forced_compaction=False,
                    replay_attempted=False,
                    persisted_rounds=self._durable_rounds(record),
                ) from exc
            carried = await self._replace_compacted_head(
                forced.new_messages,
                summary_message=forced.summary_message,
                tokens_before=forced.estimated_tokens_before,
                tokens_after=forced.estimated_tokens_after,
                outcome=forced.outcome,
                unreducible=forced.unreducible,
                trigger_tokens=forced.trigger_tokens,
                fixed_overhead_tokens=forced.fixed_overhead_tokens,
                snapshot=history,
            ) or []
            notes = self._compaction_notes(forced)
            # The marker holds the input and the rounds so far: they are durable, the replay starts clean.
            record.forced_compaction = True
            record.durable_rounds += tool_rounds(rounds)
            record.messages = []
            record.inputs = 0
        finally:
            drained = await self._close_compaction_window() or []
        for note in notes:
            await self._emit(note)
            yield note
        record.replay_attempted = True
        record.guard = self._compaction.replay_guard(self._model)
        try:
            async for ev in self._run_loop(
                # the compacted history, the lines written since it was read (mid-turn steers) and the
                # steers deferred while it ran: a steer is not left for the next turn
                history=[*forced.new_messages, *carried, *drained],
                new_messages=[],
                response_format=response_format,
                tools=tools,
                record=record,
                initial_tool_round=tool_rounds(rounds),
                budget=record.guard,
            ):
                yield ev
        except BadRequestError as replay_exc:
            if not _is_context_overflow(replay_exc):
                raise
            logger.warning(
                "AgentExecutor: the replay after a forced compaction overflowed too; recording the "
                "tool rounds the turn ran and failing the turn",
                extra={"agent_id": self._agent.id, "error": str(replay_exc)},
            )
            raise ContextOverflowUnrecoverable(
                f"the model rejected the prompt as too large, the forced compaction ran, and the "
                f"replay was rejected too: {replay_exc.message}",
                cause=replay_exc,
                forced_compaction=True,
                replay_attempted=True,
                persisted_rounds=self._durable_rounds(record),
            ) from replay_exc

    @staticmethod
    def _durable_rounds(record: _TurnRecord) -> int:
        """How many completed tool rounds of this turn are, or are about to be, in the history."""
        return record.durable_rounds + tool_rounds(completed_rounds(record.messages[record.inputs:]))

    async def _persist_failed_turn(self, record: _TurnRecord, exc: BaseException) -> None:
        """The persistence chokepoint for a turn that did not finish: write its completed rounds.

        Only WHOLE rounds (a call with its result; a half-streamed reply or a call whose dispatch never
        finished is not one), in the REDUCED form the model last saw (the replay guard's recorded
        reductions, cut further when still large, ALREADY RAN placeholders): the raw output stays in the
        event log, and persisting it raw would let the next turn overflow on it again. A hard cancel
        writes under ``asyncio.shield`` so the cancellation cannot interrupt the write. Best effort:
        a failure here (an ENDED slot, a broken mount) is logged and never masks the error that ended
        the turn.
        """
        if record.persisted:
            return
        rounds = completed_rounds(record.messages[record.inputs:])
        if not rounds:
            return
        guard = record.guard
        reduced = reduce_for_persist(
            rounds,
            sticky=guard.prune_set if guard is not None else PruneSet(),
            target_tokens=self._compaction.reduced_target(self._model) // 2,
            size=self._compaction._estimate_tokens,  # noqa: SLF001 - the strategy's own sizing
        )
        record.persisted = True
        write = self._persist_turn([*record.messages[: record.inputs], *reduced])
        try:
            if isinstance(exc, asyncio.CancelledError):
                await asyncio.shield(write)
            else:
                await write
        except Exception:  # noqa: BLE001 -- the error that ended the turn is the one to surface
            logger.exception(
                "AgentExecutor: could not record the %d tool round(s) a failed turn had completed",
                tool_rounds(rounds),
                extra={"agent_id": self._agent.id},
            )

    async def fixed_overhead_tokens(self, tools: "list[Tool] | None" = None) -> int:
        """The estimated size of what goes out on every call and no history can give back: the rendered
        system prompt and the tool catalogue (``tools``, fetched when not given)."""
        if tools is None:
            tools = await self._tool_manager.list_tools(principal=self._principal)
        return self._compaction.estimate_fixed_overhead(self._build_prompt([], []), tools)

    @staticmethod
    def _compaction_notes(compacted: "CompactedTurn") -> list[ExtendedEvent]:
        """The session-record entry for a compaction that wrote no marker and could not help.

        A compaction that summarised and was still over the trigger says so in its marker's
        payload; one that summarised nothing writes no marker, so this event (persisted as a
        ``compaction_note`` record by the dispatch path) is where it is visible."""
        if compacted.outcome != "unreducible" or compacted.unreducible is None:
            return []
        return [ExtendedEvent(extended=_CompactionNote(
            outcome=compacted.outcome,
            reason=compacted.unreducible,
            estimated_tokens=compacted.estimated_tokens_after,
            trigger_tokens=compacted.trigger_tokens,
        ))]

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

    def bind_interrupt_event(self, event: "asyncio.Event | None") -> None:
        """Bind the Stop signal: the agent loop races it against every wait for the
        model's next event (see :func:`primer.agent.loop.run_agent_turn`), so a model
        that has not produced its first token is stoppable too.

        Post-construction setter, not a constructor parameter: the caller
        (``primer.session.dispatch``) creates its cancel event only after the executor
        exists. A caller that never binds one is never stopped by it.
        """
        self._interrupt_event = event

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
        tools: "list[Tool] | None" = None,
        record: _TurnRecord | None = None,
        initial_tool_round: int = 0,
        budget: "PromptGuard | None" = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run the turn's LLM/tool loop and persist it.

        ``record`` is the turn's record (shared with ``invoke``'s persistence chokepoint and with a
        replay); a loop run on its own makes its own. ``initial_tool_round`` is the rounds the turn
        already spent in earlier attempts and ``budget`` an optional prompt guard.
        """
        from primer.agent.loop import run_agent_turn

        if record is None:
            record = _TurnRecord(messages=list(new_messages), inputs=len(new_messages))
        full_turn_messages = record.messages
        prompt = self._build_prompt(history, new_messages)
        self.was_interrupted = False
        interrupted_holder: list[bool] = []

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
                interrupt=self._interrupt_event,
                interrupted_out=interrupted_holder,
                tools=tools,
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
            exc.llm_messages = list(full_turn_messages[record.inputs:])
            raise
        except ToolWaitPark as exc:
            # 01a0518b: same stamp, same reasoning, as the YieldToWorker
            # arm above -- ToolWaitPark is deliberately NOT a
            # YieldToWorker subclass (see its own docstring), so it needs
            # its own explicit arm here too. Minimal raise-time contract
            # (loop.py's _dispatch_as_claims leaves llm_messages unset);
            # this is the "one layer up" that stamps it, mirroring
            # YieldToWorker's precedent exactly.
            exc.llm_messages = list(full_turn_messages[record.inputs:])
            raise

        # A Stop ends the loop cleanly. What was persisted is whatever COMPLETED
        # (assistant + tool rounds, always paired); the interrupted round's partial
        # assistant text never became a message, so it never reaches the history.
        self.was_interrupted = bool(interrupted_holder)

        # Persist only when the loop actually produced an assistant
        # message (helper appends it on the first non-tool stop or
        # not at all on empty/error streams).
        produced_assistant = any(
            m.role == "assistant" for m in full_turn_messages[record.inputs:]
        )
        if produced_assistant:
            record.persisted = True
            await self._persist_turn(full_turn_messages)

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
