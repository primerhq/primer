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
* Hard-overflow recovery -- catch a RAISED :class:`BadRequestError` that
  :func:`primer.common.context_overflow.is_context_overflow` classifies as
  a context overflow, force-compact, and CONTINUE the turn once. A YIELDED
  overflow (Ollama, Gemini deliver the 400 as a fatal ``Error`` event) is
  recovered the same way: the loop holds the ``Error`` back instead of
  yielding it (a yielded one is a terminal record, and a recovered turn
  must end with exactly one) and raises ``TurnStreamOverflow``, which
  :meth:`_overflow_of` turns into the ``BadRequestError`` the recovery
  works on.

Subclasses provide three abstract hooks:

* :meth:`_load_history`
* :meth:`_persist_turn`
* :meth:`_replace_compacted_head`
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
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
from primer.agent.overflow import cap_newest_round, completed_rounds, kept_rounds, reduce_for_persist, tool_rounds
from primer.agent.prompt_render import render_system_prompt_or_raw
from primer.agent.prune import PruneSet
from primer.agent.tail import pending_from
from primer.agent.tool_manager import ToolExecutionManager
from primer.common.context_overflow import is_context_overflow, output_cap_never_fits, output_cap_warning
from primer.model.chat import (
    CompactionSummary,
    ExtendedEvent,
    Message,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    TurnStreamOverflow,
    Usage,
    _CompactionNote,
    output_to_message,
)
from primer.model.except_ import (
    AuthRequiredError,
    BadRequestError,
    ContextOverflowUnrecoverable,
    PrimerError,
    SummariserOverflow,
)
from primer.model.graph import build_execution_context
from primer.model.yield_ import CANCEL_REASON_PREEMPTED


if TYPE_CHECKING:
    from collections.abc import Callable

    from primer.agent.compaction import CompactedTurn
    from primer.model.chat import Tool

    from primer.int.artifact_storage import ArtifactStorage
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model.yield_ import ToolWaitPark, YieldToWorker
    from primer.model_profile import ResolvedModel


logger = logging.getLogger(__name__)


#: How long a cancelled turn waits for its forced compaction's marker commit to finish before it stops waiting (the
#: commit itself is not cancelled): bounded so a drain can abort a turn stuck on a dead storage.
_MARKER_COMMIT_GRACE_S = 30.0


def _consume_abandoned_commit(commit: "asyncio.Future", rounds_lost: int | None = None) -> None:
    """Retrieve what an abandoned marker commit dies of, so it is logged and not "never retrieved".

    ``rounds_lost`` is set when the turn TOOK the commit as landed and dropped its rounds from the record: a commit
    that then fails (or is cancelled) means those rounds are in neither the marker nor messages.jsonl, and the next
    turn runs those tool calls again. It is the number of tool rounds that went with it, and it is logged at ERROR.
    """
    if rounds_lost is not None:
        if commit.cancelled() or commit.exception() is not None:
            logger.error(
                "AgentExecutor: the compaction marker commit, taken as landed when the turn was cancelled, did not "
                "land: the turn's %d completed tool round(s) are in neither the marker nor messages.jsonl, so the "
                "next turn runs those tool calls again",
                rounds_lost, exc_info=None if commit.cancelled() else commit.exception(),
            )
        return
    if not commit.cancelled() and commit.exception() is not None:
        logger.warning("AgentExecutor: the abandoned compaction marker commit failed", exc_info=commit.exception())


#: The compaction-window closes a cancelled turn left to run after its abandoned marker commit (held so the task is not
#: garbage collected before it has run).
_DEFERRED_WINDOW_CLOSES: "set[asyncio.Future]" = set()


def _deferred_window_close_done(task: "asyncio.Future") -> None:
    _DEFERRED_WINDOW_CLOSES.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("AgentExecutor: closing the compaction window after an abandoned commit failed", exc_info=task.exception())


def refuse_compaction_summaries(messages: "list[Message]", where: str) -> None:
    """A :class:`CompactionSummary` is a structural tag on an ordinary message: it does not survive JSON, so one that
    is persisted as a message line or stamped into a parked state comes back as a reply the model wrote, and the
    compactor no longer looks through it (``pending_from``). Its durable form is the compaction marker. Nothing writes
    one today; this fails loudly if something ever does, instead of corrupting the next turn."""
    if any(isinstance(m, CompactionSummary) for m in messages):
        raise ValueError(
            f"a CompactionSummary reached {where}: the summary of a compaction is durable only as a compaction "
            "marker, and the tag that tells it from a reply does not survive being written as a message line"
        )


@dataclass
class _TurnRecord:
    """What one ``invoke`` has produced so far, shared by its attempts.

    ``messages`` is the turn's own messages: its input (``inputs`` leading entries) and then every
    round the model and the tools completed, appended by the loop as they finish. It is the one
    thing the persistence chokepoint reads when a turn ends any way but a park or a normal finish.
    The overflow recovery folds the rounds so far into the history through a compaction marker, so they
    are not written twice: ``kept_rounds`` of them are in the marker's kept tail as messages,
    ``summarised_rounds`` only as part of its summary. ``base`` is the history the replay is sent
    (what the recorded prune set was measured against).
    """

    messages: list[Message]
    inputs: int
    persisted: bool = False
    kept_rounds: int = 0
    summarised_rounds: int = 0
    base: list[Message] = field(default_factory=list)
    guard: Any = None
    forced_compaction: bool = False
    replay_attempted: bool = False
    notes: list[ExtendedEvent] = field(default_factory=list)
    #: A cancel that said the lease was lost (``CANCEL_REASON_PREEMPTED``) reached this turn. Sticky on the turn and
    #: not only on the exception: a later cancel (the drain's) can replace the exception while the turn unwinds, and
    #: the chokepoint must still write nothing, because the session may belong to another worker.
    lease_lost: bool = False


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
        # The park a Stop ended in the LATEST invoke() (see run_agent_turn's ``stopped_park_out``), else None. Dispatch
        # reads it in the cancelled exit: what the park's tool had already created (an external tool's pending call
        # row) is cleaned up there. Reset at the start of every invoke.
        self.stopped_park: "YieldToWorker | ToolWaitPark | None" = None
        # The tool calls the Stop CANCELLED or ABANDONED in the LATEST invoke() (see run_agent_turn's
        # ``stopped_calls_out``). Dispatch reads it in the cancelled exit: a call cancelled after its tool wrote
        # something before it could yield (an external tool's pending call row) never reaches the yield. Reset at the
        # start of every invoke.
        self.stopped_calls: "list[Any]" = []
        # True when the LATEST invoke() ended because the model asked for another tool
        # round at ``agent.max_tool_turns``. The model's last event is still
        # Done(tool_use), so the post-turn status mapper cannot tell this from a turn
        # that is mid-chain without this flag. Reset like ``was_interrupted``.
        self.hit_tool_turn_cap: bool = False
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
        summary_input_reduced: dict[str, int] | None = None,
        snapshot: list[Message] | None = None,
    ) -> list[Message] | None:
        """Replace the persisted history with the compacted form.

        ``summary_message`` is the assistant-role summary the strategy
        produced (``None`` for pruning-only compaction, where nothing was
        summarised); ``tokens_before`` / ``tokens_after`` are the strategy's
        telemetry, and ``outcome`` / ``unreducible`` / ``trigger_tokens`` its
        verdict (``insufficient`` when the summary stands and the prompt is
        still over the trigger) and ``fixed_overhead_tokens`` the part of the
        prompt no history can give back that its figures include. ``summary_input_reduced`` is what the
        compaction did to the summariser's input when its first call overflowed (``None`` when it did not).
        ``snapshot`` is the history the compaction
        was computed from: lines written to the persisted history after it
        was taken (a steer, say) are not in ``compacted`` and must survive
        the fold; the hook returns those lines (``None`` or empty when there are
        none) so the caller can hand them to the turn that follows. Surfaces that record compaction as an append-only marker
        (the workspace executor) use these to build the marker payload;
        surfaces that rewrite in place (the chat/thread executor) ignore them.
        """

    async def _last_compaction(self) -> tuple[int | None, tuple[str, str] | None]:
        """``(tokens_after, noted)`` for the newest compaction. Default: ``(None, None)``, nothing known.

        ``tokens_after`` is the estimated prompt size it left: the strategy uses it to hold off compacting
        again until the prompt has grown (a compaction that cannot reach its trigger would otherwise
        summarise its own summary every turn). ``noted`` is the ``(outcome, reason)`` of the newest note in
        the session record since then, so a run of the same verdict is noted once. The workspace executor
        reads both back from ``messages.jsonl``."""
        return None, None

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
        # An output cap that fills the window can never be served by a provider that checks it. The reactive guard in
        # ``_recover_from_overflow`` acts only AFTER a rejection, so say it up front, once per turn, and run the turn
        # anyway (a server that clamps the cap serves it). An aggregated profile (no provider of its own) is skipped:
        # the window known here is the MIN over its members, and a cap between the windows is served by a larger one.
        if self._model.provider_id is not None:
            cap_warning = output_cap_warning(self._agent.max_output_tokens, self._model.context_length)
            if cap_warning is not None:
                logger.warning(
                    "AgentExecutor: the agent's output cap is not below the model's context window; the turn runs "
                    "anyway (a provider that checks the cap rejects it)",
                    extra={
                        "agent_id": self._agent.id,
                        "max_output_tokens": self._agent.max_output_tokens,
                        "context_length": self._model.context_length,
                        "detail": cap_warning,
                    },
                )
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
            last_compaction_tokens, noted = await self._last_compaction()
            compacted = await self._compaction.maybe_compact(
                agent=self._agent,
                llm=self._llm,
                model=self._model,
                history=history,
                new_messages=messages,
                fixed_overhead=fixed_overhead,
                last_compaction_tokens=last_compaction_tokens,
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
                    summary_input_reduced=compacted.summary_input_reduced,
                    snapshot=history,
                )
                notes += self._compaction_notes(compacted, noted=noted)
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
            except (BadRequestError, TurnStreamOverflow) as caught:
                # A RAISED BadRequestError (Anthropic, OpenAI-family) or a YIELDED overflow the loop held back
                # (Ollama, Gemini: TurnStreamOverflow). Any other failure is not this recovery's.
                overflow = self._overflow_of(caught)
                if overflow is None:
                    raise
                async for ev in self._recover_from_overflow(
                    overflow,
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

        Not when the agent's own ``max_output_tokens`` is not below the model's context window: no history,
        however short, fits beside that cap, so the rejection is the cap talking and a compaction would only
        rewrite the persisted history into a summary before the replay is rejected the same way. This is
        decided from the configuration, before anything else, so it holds whatever the provider's rejection
        says (a provider code, or a text the classifier takes for an input overflow, is no proof the history
        is what is too big); the message-level arithmetic of the classifier only covers providers that state
        both numbers. The rounds the turn completed are not lost: nothing was folded, so they stay in the
        record and the chokepoint writes them on the way out.
        """
        max_output = self._agent.max_output_tokens
        if output_cap_never_fits(max_output, self._model.context_length):
            logger.warning(
                "AgentExecutor: hard-overflow detected, but the agent's output cap fills the context window; "
                "not compacting",
                extra={
                    "agent_id": self._agent.id,
                    "max_output_tokens": max_output,
                    "context_length": self._model.context_length,
                    "error": str(exc),
                },
            )
            raise ContextOverflowUnrecoverable(
                f"the model rejected the prompt as too large, but the agent's max_output_tokens ({max_output}) "
                f"is not below the model's context window ({self._model.context_length}): no history, however "
                f"short, fits beside that cap, so compaction cannot help; lower max_output_tokens or use a "
                f"model with a larger window",
                cause=exc,
                forced_compaction=False,
                replay_attempted=False,
                persisted_rounds=self._persisted_rounds(record),
            ) from exc
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
        # The forced compaction keeps the opening user input and the NEWEST folded round whole, so with the
        # fixed part counted they have to fit what the budget leaves: cut the newest round (its results
        # already ran, so it is reducible) rather than let the compaction declare it protected_over_budget.
        base = [*history, *messages]
        protected = size([m for m in base[pending_from(base):] if m.role == "user"])
        reduced = cap_newest_round(
            reduced,
            cap_tokens=self._compaction.newest_round_cap(
                self._model, fixed_overhead=fixed_overhead, protected_tokens=protected,
            ),
            size=size,
        )
        notes: list[ExtendedEvent] = []
        carried: list[Message] = []
        drained: list[Message] = []
        abandoned: "asyncio.Future | None" = None   # a marker commit this turn stopped waiting for (see ``finally``)
        # Hard-overflow recovery runs an LLM await (force_compact), so bracket it with the window too.
        await self._open_compaction_window()
        try:
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
            except SummariserOverflow as failed:
                failed.forced_compaction = True  # it ran: its summariser is what could not be made to fit
                raise
            changed = reduced != rounds       # our own reduction of the rounds the turn ran made a smaller prompt
            if self._replay_is_futile(forced, changed=changed):
                # Nothing can be shrunk (the fixed part, or the input the model has not answered, fills the
                # window, or there is nothing before the question to summarise and nothing was reduced): the
                # replay would send the prompt that was just rejected, so fail now, with a name, instead of
                # spending a model call on it. The rounds the turn completed are not lost: no marker was
                # written, so they stay in the record and the chokepoint writes them on the way out.
                before = size([*history, *messages, *rounds]) + fixed_overhead
                undercounted = (
                    not changed
                    and forced.budget_tokens is not None
                    and forced.estimated_tokens_after < forced.budget_tokens
                )
                raise ContextOverflowUnrecoverable(
                    f"the model rejected the prompt as too large and compaction cannot shrink it "
                    f"({forced.unreducible}): about {forced.estimated_tokens_after} tokens by our estimate"
                    + (
                        f" ({before} before the {tool_rounds(rounds)} completed tool round(s) were reduced)"
                        if changed else ""
                    )
                    + f", of which {forced.fixed_overhead_tokens} are the system prompt and tool schemas, "
                    f"against a context window of {self._model.context_length}"
                    + (
                        "; the estimate is under the budget, so it undercounts what the provider counted "
                        "(images, documents and dense text are the usual causes)"
                        if undercounted else ""
                    ),
                    cause=exc,
                    forced_compaction=True,
                    replay_attempted=False,
                    persisted_rounds=self._persisted_rounds(record),
                ) from exc
            def fold_into_record() -> None:
                """The marker holds the input and the rounds so far (the newest ones as messages, the rest in its
                summary): they are durable, so the record lets them go and the replay starts clean."""
                kept = kept_rounds(forced.new_messages, reduced)
                record.kept_rounds += kept
                record.summarised_rounds += tool_rounds(rounds) - kept
                record.messages = []
                record.inputs = 0

            # The marker commit runs under a shield. A hard cancel that lands on this await must not leave
            # the commit half-accounted: if it lands anyway, the rounds are in the marker, and a record that
            # still held them would write them again on the way out (every tool_use id twice, a 400 on every
            # later request). So the commit is allowed to finish, and the record is told, before the cancel
            # goes on.
            write = asyncio.ensure_future(self._replace_compacted_head(
                forced.new_messages,
                summary_message=forced.summary_message,
                tokens_before=forced.estimated_tokens_before,
                tokens_after=forced.estimated_tokens_after,
                outcome=forced.outcome,
                unreducible=forced.unreducible,
                trigger_tokens=forced.trigger_tokens,
                fixed_overhead_tokens=forced.fixed_overhead_tokens,
                summary_input_reduced=forced.summary_input_reduced,
                snapshot=history,
            ))
            def lease_lost() -> None:
                """A cancel that says the lease was lost reached the wait: the session may belong to another worker,
                and the chokepoint writes no rounds for it (``_write_failed_rounds``). What the wait would settle,
                whether the record still holds rounds the marker has, is moot, so this is the one kind of cancel that
                is not held up, whether it is the first or arrives while an earlier one is waiting. It is remembered
                on the turn (a later cancel can replace the exception), and the commit is left to finish alone."""
                nonlocal abandoned
                record.lease_lost = True
                write.add_done_callback(_consume_abandoned_commit)
                abandoned = write
                if not write.done():
                    logger.warning(
                        "AgentExecutor: the lease was lost while the compaction marker commit was still running; "
                        "leaving it to finish on its own (the window closes, and the steers deferred meanwhile are "
                        "applied, once it is done)",
                        extra={"agent_id": self._agent.id},
                    )

            try:
                carried = await asyncio.shield(write) or []
            except asyncio.CancelledError as cancelled:
                if cancelled.args[:1] == (CANCEL_REASON_PREEMPTED,):
                    lease_lost()
                    raise
                # Wait for the commit to be DONE, through any further cancel: a cancel that lands on this wait
                # cancels the await and not the write (a thread writes the marker and it lands whatever the task
                # does), so reading it as "the commit failed" would leave the record holding rounds the marker has.
                # The wait is BOUNDED, like the dispatch's own shelter for a cancelled exit
                # (``_finish_despite_cancel``): a drain must still be able to abort a turn whose commit hangs on a
                # dead storage or workspace.
                loop = asyncio.get_running_loop()
                deadline = loop.time() + _MARKER_COMMIT_GRACE_S
                while not write.done():
                    try:
                        # ``asyncio.wait`` does not cancel what it waits on when this task is cancelled
                        await asyncio.wait({write}, timeout=max(0.0, deadline - loop.time()))
                    except asyncio.CancelledError as again:
                        if again.args[:1] == (CANCEL_REASON_PREEMPTED,):
                            lease_lost()
                            raise           # in place of the first cancel: the exception the turn ends with says why
                        continue
                    if not write.done():
                        break                   # the grace is up
                if not write.done():
                    # Unknown outcome. The commit is a thread that may still land: assume it will, because writing
                    # the rounds again would put every tool_use id in the history twice (a 400 on every later
                    # request), whereas rounds that never land are only run again.
                    abandoned = write
                    write.add_done_callback(functools.partial(
                        _consume_abandoned_commit,
                        rounds_lost=tool_rounds(rounds) if forced.summary_message is not None else None,
                    ))
                    logger.error(
                        "AgentExecutor: the compaction marker commit did not finish within %gs of the cancel; "
                        "assuming it lands, and not writing the turn's rounds a second time",
                        _MARKER_COMMIT_GRACE_S, extra={"agent_id": self._agent.id},
                    )
                    if forced.summary_message is not None:
                        fold_into_record()
                elif write.cancelled() or write.exception() is not None:
                    logger.warning(
                        "AgentExecutor: the compaction marker commit failed while the turn was being cancelled; "
                        "the turn's rounds stay in the record",
                        extra={"agent_id": self._agent.id},
                        exc_info=None if write.cancelled() else write.exception(),
                    )
                elif forced.summary_message is not None:
                    fold_into_record()
                raise cancelled
            notes = self._compaction_notes(forced)
            record.forced_compaction = True
            if forced.summary_message is not None:
                fold_into_record()
            else:
                # Nothing was summarised, so no marker holds the input and the rounds: they stay in the record,
                # in the reduced form the replay is sent, and the chokepoint or a normal finish writes them.
                record.messages = [*record.messages[: record.inputs], *reduced]
        finally:
            if abandoned is not None and not abandoned.done():
                # The commit this turn stopped waiting for still holds the messages lock, and closing the window
                # takes it: awaiting it here would keep a cancelled turn running for as long as the commit hangs,
                # which is what the bound on the wait is for. The window closes when the commit is done (the steers
                # deferred meanwhile stay queued, by design, and are applied then).
                self._close_the_window_when_done(abandoned)
            else:
                drained = await self._close_compaction_window() or []
        for note in notes:
            await self._emit(note)
            yield note
        record.replay_attempted = True
        record.guard = self._compaction.replay_guard(self._model, fixed_overhead=fixed_overhead)
        # the compacted history, the lines written since it was read (mid-turn steers) and the steers
        # deferred while it ran: a steer is not left for the next turn
        replay_history = [*forced.new_messages, *carried, *drained]
        # what the guard's recorded reductions are keyed against: the prompt before the record's own rounds
        # (without a marker the reduced rounds are in the record AND at the end of the compacted history)
        record.base = (
            replay_history if forced.summary_message is not None
            else [*forced.new_messages[: len(forced.new_messages) - len(reduced)], *carried, *drained]
        )
        try:
            async for ev in self._run_loop(
                history=replay_history,
                new_messages=[],
                response_format=response_format,
                tools=tools,
                record=record,
                initial_tool_round=tool_rounds(rounds),
                budget=record.guard,
            ):
                yield ev
        except (BadRequestError, TurnStreamOverflow) as caught:
            replay_exc = self._overflow_of(caught)
            if replay_exc is None:
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
                persisted_rounds=self._persisted_rounds(record),
            ) from replay_exc

    @staticmethod
    def _overflow_of(caught: BaseException) -> BadRequestError | None:
        """The overflow ``caught`` stands for, as the ``BadRequestError`` the recovery works on; ``None`` if it
        is not one. A raised ``BadRequestError`` is itself, when the classifier says it is a context overflow.
        A ``TurnStreamOverflow`` is the same 400 delivered as a yielded ``Error`` (already classified by the
        loop), rebuilt as the exception a raising adapter would have sent: its message and code are the
        provider's, and it is what the typed failure carries as ``__cause__``."""
        if isinstance(caught, TurnStreamOverflow):
            return BadRequestError(
                caught.error.message, code=caught.error.code or "bad_request", status_code=400,
            )
        if isinstance(caught, BadRequestError) and is_context_overflow(caught):
            return caught
        return None

    @staticmethod
    def _persisted_rounds(record: _TurnRecord) -> int:
        """How many completed tool rounds of this turn are, or are about to be, in the history as messages:
        the ones the compaction marker kept and the ones the chokepoint is about to write. (The failure
        sets the figure again once the write has happened or failed.)"""
        return record.kept_rounds + tool_rounds(completed_rounds(record.messages[record.inputs:]))

    def _close_the_window_when_done(self, commit: "asyncio.Future") -> None:
        """Close the compaction window once ``commit`` (which holds the messages lock) is done, without waiting for it."""

        def start(_commit: "asyncio.Future") -> None:
            task = asyncio.ensure_future(self._close_compaction_window())
            _DEFERRED_WINDOW_CLOSES.add(task)
            task.add_done_callback(_deferred_window_close_done)

        commit.add_done_callback(start)

    async def _persist_failed_turn(self, record: _TurnRecord, exc: BaseException) -> None:
        """The persistence chokepoint for a turn that did not finish: write its completed rounds.

        Only WHOLE rounds (a call with its result; a half-streamed reply or a call whose dispatch never
        finished is not one), in the REDUCED form the model last saw (the replay guard's recorded
        reductions, cut further when still large, ALREADY RAN placeholders): the raw output stays in the
        event log, and persisting it raw would let the next turn overflow on it again. A hard cancel
        writes under ``asyncio.shield`` so the cancellation cannot interrupt the write, except one that
        says the lease was lost (``CANCEL_REASON_PREEMPTED``): the session may belong to another worker
        by then and a write from here could interleave with its own. Best effort: a failure here (an
        ENDED slot, a broken mount) is logged and never masks the error that ended the turn.

        A :class:`ContextOverflowUnrecoverable` is told afterwards how many rounds really are in the
        history as messages and how many only in a summary, so what the ERROR record says is what
        happened, not what was about to.
        """
        written = 0
        try:
            written = await self._write_failed_rounds(record, exc)
        finally:
            if isinstance(exc, ContextOverflowUnrecoverable):
                exc.persisted_rounds = record.kept_rounds + written
                exc.summarised_rounds = record.summarised_rounds

    async def _write_failed_rounds(self, record: _TurnRecord, exc: BaseException) -> int:
        """Write the completed rounds of a failed turn; the number of rounds actually written."""
        if record.persisted:
            return 0
        rounds = completed_rounds(record.messages[record.inputs:])
        if not rounds:
            return 0
        if record.lease_lost or (isinstance(exc, asyncio.CancelledError) and exc.args[:1] == (CANCEL_REASON_PREEMPTED,)):
            logger.warning(
                "AgentExecutor: the lease was lost; not recording the %d tool round(s) the turn had completed",
                tool_rounds(rounds),
                extra={"agent_id": self._agent.id},
            )
            return 0
        guard = record.guard
        reduced = reduce_for_persist(
            rounds,
            sticky=guard.prune_set if guard is not None else PruneSet(),
            target_tokens=self._compaction.reduced_target(self._model) // 2,
            size=self._compaction._estimate_tokens,  # noqa: SLF001 - the strategy's own sizing
            context=record.base,
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
            return 0
        return tool_rounds(rounds)

    async def fixed_overhead_tokens(self, tools: "list[Tool] | None" = None) -> int:
        """The estimated size of what goes out on every call and no history can give back: the rendered
        system prompt and the tool catalogue (``tools``, fetched when not given)."""
        if tools is None:
            tools = await self._tool_manager.list_tools(principal=self._principal)
        return self._compaction.estimate_fixed_overhead(self._build_prompt([], []), tools)

    @staticmethod
    def _replay_is_futile(forced: "CompactedTurn", *, changed: bool = False) -> bool:
        """Whether a forced compaction that came back ``unreducible`` ends the turn instead of replaying it.

        ``fixed_over_budget`` and ``protected_over_budget`` always do: the compaction itself judged that the
        part no history can give back (the system prompt and the tool schemas, or those plus the input the
        model has not answered and the newest round) fills the budget.

        ``empty_head`` (nothing precedes the protected part, so nothing could be summarised) does when the
        replay would send the prompt the provider just rejected: the tier-1 prune changed no tool output and
        neither did the caller (``changed``: the rounds the turn ran were reduced before the compaction, as
        ALREADY RAN placeholders, so the replay is a smaller prompt than the one that was rejected and gets its
        call; a fresh session whose first round was huge is exactly that case). Our own estimate does NOT
        decide it: an estimate under the budget only says the heuristic undercounts what the provider counted
        (an image or a document is a flat guess, dense text runs over chars/4), and a byte-identical prompt is
        rejected again whatever the estimate says. The error says so when they disagree."""
        if forced.outcome != "unreducible":
            return False
        if forced.unreducible in ("fixed_over_budget", "protected_over_budget"):
            return True
        return forced.pruned_tool_outputs == 0 and not changed

    @staticmethod
    def _compaction_notes(
        compacted: "CompactedTurn", *, noted: tuple[str, str] | None = None,
    ) -> list[ExtendedEvent]:
        """The session-record entry for a compaction that wrote no marker.

        A compaction that summarised and was still over the trigger says so in its marker's
        payload; one that summarised nothing writes no marker, so this event (persisted as a
        ``compaction_note`` record by the dispatch path) is where it is visible: ``unreducible``
        (it could not help) and ``skipped`` (it deliberately did nothing, because it could not
        reach the trigger). Either repeats every turn until something changes, so a verdict is noted once
        per run (``noted``: the newest note since the last marker already says this outcome and reason; a
        different reason is a new run)."""
        if compacted.unreducible is None or compacted.outcome not in ("unreducible", "skipped"):
            return []
        if noted == (compacted.outcome, compacted.unreducible):
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
        self.stopped_park = None
        self.stopped_calls = []
        self.hit_tool_turn_cap = False
        interrupted_holder: list[bool] = []
        capped_holder: list[bool] = []
        stopped_park_holder: list[Any] = []
        stopped_calls_holder: list[Any] = []

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
                capped_out=capped_holder,
                stopped_park_out=stopped_park_holder,
                stopped_calls_out=stopped_calls_holder,
                tools=tools,
                budget=budget,
                initial_tool_round=initial_tool_round,
                intercept_context_overflow=True,
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
            refuse_compaction_summaries(exc.llm_messages, "a parked state")
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
            refuse_compaction_summaries(exc.llm_messages, "a parked state")
            raise

        # A Stop ends the loop cleanly. What was persisted is whatever COMPLETED
        # (assistant + tool rounds, always paired); the interrupted round's partial
        # assistant text never became a message, so it never reaches the history.
        self.was_interrupted = bool(interrupted_holder)
        self.stopped_park = stopped_park_holder[0] if stopped_park_holder else None
        self.stopped_calls = list(stopped_calls_holder)
        self.hit_tool_turn_cap = bool(capped_holder)

        # Persist only when the loop actually produced an assistant
        # message (helper appends it on the first non-tool stop or
        # not at all on empty/error streams).
        produced_assistant = any(
            m.role == "assistant" for m in full_turn_messages[record.inputs:]
        )
        if produced_assistant:
            record.persisted = True
            await self._persist_turn(full_turn_messages)

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
