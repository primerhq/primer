"""Shared single-turn agent loop.

Extracts the inner LLM + tool-dispatch loop from
:class:`_BaseAgentExecutor` so both the agent executor (chat /
workspace) and the graph executor (per-node invocation) share the
same logic. Keeps the project's agent-loop semantics consistent
no matter which entry point invoked the agent.

Behaviour:

* Calls ``llm.stream(...)`` with the supplied prompt, ``response_format``,
  and the tool catalogue from the supplied :class:`ToolExecutionManager`.
* Yields every event live (no buffering at this layer).
* When the assistant emits :class:`ToolCallPart`s, dispatches each via
  the manager, synthesises an :class:`ExtendedEvent(_ExecutorToolResult)`
  for taps, and re-arms the LLM call with the tool-result messages
  appended.
* Loops until the assistant produces a non-tool stop OR the LLM stream
  yields no convertible events (empty / error stream).
* Writes the assistant + tool-result messages into the caller-provided
  ``messages_out`` list (mutated in place).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, Protocol

import primer.observability.metrics as _metrics

from primer.agent.interrupt import Interrupted, interruptible
from primer.agent.stoppable_call import run_stoppable
from primer.agent.tool_manager import ToolExecutionManager
from primer.common.context_overflow import is_context_overflow_error
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.media.hydrate import hydrate_prompt_parts
from primer.model.chat import (
    Done,
    Error,
    ExtendedEvent,
    Message,
    StreamEvent,
    Tool,
    ToolCallPart,
    ToolResultPart,
    TurnStreamFailure,
    TurnStreamOverflow,
    Usage,
    output_to_message,
    _ClientAction,
    _ExecutorToolResult,
    _LlmCall,
)
from primer.model.except_ import AuthRequiredError, PrimerError
from primer.model.yield_ import ToolWaitPark, YieldToWorker, asks_a_person


if TYPE_CHECKING:
    from primer.int.artifact_storage import ArtifactStorage
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model_profile import ResolvedModel


logger = logging.getLogger(__name__)

# The synthetic result a tool call gets when a Stop landed before the loop dispatched its round.
_STOPPED_REFUSAL = "not run: stopped by user"
# ... and when the round it asked for is the one that hit ``max_tool_turns``.
_TOOL_CAP_REFUSAL = "not executed: tool-turn cap reached"
# ... and when a Stop landed while the call was RUNNING and it has no result to report: it asked to park, or it was
# cancelled (or abandoned) by the Stop (slice B1). It started and never reported a result, so it cannot say "not
# run". One wording for all three. (The calls that finished before it keep their real results.)
_PARK_STOPPED_REFUSAL = "interrupted: stopped by user (the call may have run, and its result was not recorded)"


def _answer_undispatched(
    tool_calls: list[ToolCallPart],
    text: str,
    messages_out: list[Message] | None,
    *,
    results: Mapping[str, ToolResultPart] | None = None,
) -> Iterator[ExtendedEvent]:
    """Answer every call of a round the loop is NOT going to run with a synthetic ERROR result.

    ``results`` gives the answer for the calls that have one of their own (a notifying call that already ran keeps
    its real result; a call that was running when a Stop ended its park says so); every other call gets ``text``.

    A tool call that no result answers leaves the persisted history invalid for the provider (Anthropic
    400s every later request, OpenAI Chat Completions rejects an assistant ``tool_calls`` no tool message
    follows), so a turn that ends without dispatching a round must still answer it. This does BOTH halves
    so a caller cannot do one and forget the other: it appends the one ``tool`` message to ``messages_out``
    (the model's history; first, before any event is yielded, so closing the generator early cannot leave
    the history unpaired) and yields an ``_ExecutorToolResult`` event per call (the durable log). Callers
    iterate it and yield what it yields: ``for ev in _answer_undispatched(...): yield ev``. An error
    result, rather than dropping the call, keeps the model told that it asked and was refused.
    """
    own = results or {}
    parts = [own.get(call.id) or ToolResultPart(id=call.id, output=text, error=True) for call in tool_calls]
    if messages_out is not None:
        messages_out.append(Message(role="tool", parts=parts))
    for part in parts:
        yield ExtendedEvent(
            extended=_ExecutorToolResult(call_id=part.id, output=part.output, error=part.error, metadata=part.metadata)
        )


def _answer_a_stopped_park(
    park: "YieldToWorker | ToolWaitPark",
    tool_calls: list[ToolCallPart],
    client_actions: "list[_ClientAction]",
    messages_out: list[Message] | None,
) -> Iterator[ExtendedEvent]:
    """Answer the round whose dispatch ended in a park that a Stop has since made pointless.

    The client actions the batch already delivered go out first (tool_call -> client_action -> tool_result, as in the
    normal path). For a ``tool_wait`` batch the notifying calls already RAN and keep their real results, and the
    claimable ones never started. For an in-process park the dispatch stamped the exception with the position of the
    call that asked to wait and the results of the calls that had finished before it
    (:func:`_dispatch_tool_calls`): those keep their REAL results, the call that asked to wait is answered "may have
    run" (it was running and may have had effects it never reported), and the calls after it never started. The
    position, not ``park.tool_call_id``, finds the yielding call: for a nested yield (invoke_agent, invoke_graph) that
    id is the INNER call's raw provider id, which restarts every stream and can equal an earlier outer id. With no
    stamp every call is answered "may have run": never "not run" for a call that might have run.
    """
    for action in client_actions:
        yield ExtendedEvent(extended=action)
    own: dict[str, ToolResultPart] = {}
    if isinstance(park, ToolWaitPark):
        own = {result.id: result for _scoped_id, result in park.notifying_results}
    else:
        index = park.batch_index
        if index is None or not 0 <= index < len(tool_calls):
            own = {call.id: ToolResultPart(id=call.id, output=_PARK_STOPPED_REFUSAL, error=True) for call in tool_calls}
        else:
            own = {part.id: part for part in park.completed_results or []}
            yielding = tool_calls[index]
            own[yielding.id] = ToolResultPart(id=yielding.id, output=_PARK_STOPPED_REFUSAL, error=True)
    yield from _answer_undispatched(tool_calls, _STOPPED_REFUSAL, messages_out, results=own)


class PromptGuard(Protocol):
    """The seam through which a caller may reduce the prompt before each model call.

    ``run_agent_turn`` calls :meth:`before_call` with the accumulated prompt and
    the tool catalogue immediately before EVERY ``llm.stream`` call (the first
    one included) and sends what it returns; it calls :meth:`after_call` with the
    call's ``Usage`` (``None`` when the provider reported none) once the call has
    finished. The guard sees and returns only the OUTGOING prompt: ``messages_out``
    (what the caller persists and what a park stamps onto its exception) is never
    touched, so a reduction is ephemeral and the durable record stays raw.

    The loop owns no policy: a guard that reduces nothing is the default (no guard
    at all), and later slices supply the implementation. Keep this surface small;
    it is the one place the loop knows a guard exists.
    """

    async def before_call(
        self, prompt: list[Message], *, tools: list[Tool],
    ) -> list[Message]: ...

    def after_call(self, usage: "Usage | None") -> None: ...


def _observe_llm_call(
    llm_model: "ResolvedModel",
    t0: float,
    usage: "Usage | None",
    status: str,
) -> float:
    """Record one model call against the per-profile instruments.

    This loop is the ONE model-call seam every executor shares (the base
    agent executor, the graph agent node and the subagent runner all call
    run_agent_turn), so instrumenting here counts every call exactly once.
    Returns the elapsed seconds so the caller can reuse them.
    """
    elapsed = time.monotonic() - t0
    # llm_model.provider_id is None for an aggregated profile (01a067c4);
    # prometheus_client tolerates None (coerces to the literal string
    # "None") but that defeats the label's purpose, so fall back to the
    # profile id, same as the trace-row display consumers.
    _metrics.llm_calls_total.labels(
        llm_model.provider_id or llm_model.profile_id, llm_model.profile_id, status,
    ).inc()
    if usage is not None:
        if usage.input_tokens:
            _metrics.llm_profile_tokens_total.labels(
                llm_model.profile_id, "in",
            ).inc(usage.input_tokens)
        if usage.output_tokens:
            _metrics.llm_profile_tokens_total.labels(
                llm_model.profile_id, "out",
            ).inc(usage.output_tokens)
    return elapsed


def _observe_prompt_estimate(
    llm_model: "ResolvedModel",
    usage: "Usage | None",
    prompt: list[Message],
    tools: list["Tool"],
) -> int | None:
    """Compare the provider's count of the prompt it was sent with our heuristic estimate of it.

    Returns the estimate and records ``usage.input_tokens / estimate`` on ``llm_prompt_estimate_ratio`` for a
    call that came back with usage; returns ``None`` and records NOTHING (not a zero) for one that did not,
    and does not even pass over the prompt then. The estimate is the figure the compaction trigger computes
    (the same per-part heuristic over the system prompt, the history and the tool schemas), taken from the
    prompt as SENT: after a guard reduced it, hydrated, whatever the provider actually received. Provider
    usage is free, so this costs one local pass over the prompt and no counting; it changes no decision.
    """
    if usage is None or not usage.input_tokens:
        return None
    estimate = count_tokens_char_fallback(messages=prompt, tools=tools or None)
    if estimate <= 0:
        return None
    _metrics.llm_prompt_estimate_ratio.labels(
        llm_model.provider_id or llm_model.profile_id,
    ).observe(usage.input_tokens / estimate)
    return estimate


async def _emit_llm_called(
    tool_manager: ToolExecutionManager,
    llm_model: "ResolvedModel",
    call_usage: "Usage | None",
    elapsed: float,
    status: str,
) -> None:
    """Land one ``llm.called`` on the platform event log (when wired).

    Same seam argument as :func:`_observe_llm_call`: every executor
    shares this loop, so emitting here counts every provider call
    exactly once - nested subagents included.
    """
    recorder = getattr(tool_manager, "event_recorder", None)
    if recorder is None:
        return
    session_id, workspace_id = tool_manager.workspace_session_scope
    await recorder.emit(
        "llm.called",
        session_id=session_id,
        workspace_id=workspace_id,
        payload={
            "profile_id": llm_model.profile_id,
            "provider_id": llm_model.provider_id,
            "model": llm_model.model_name,
            "input_tokens": call_usage.input_tokens if call_usage else None,
            "output_tokens": call_usage.output_tokens if call_usage else None,
            "duration_ms": max(0, int(elapsed * 1000)),
            "status": status,
        },
    )


async def run_agent_turn(
    *,
    agent: "Agent",
    llm: "LLM",
    llm_model: "ResolvedModel",
    tool_manager: ToolExecutionManager,
    prompt: list[Message],
    response_format: dict[str, Any] | None = None,
    principal: str | None = None,
    messages_out: list[Message] | None = None,
    artifact_storage: "ArtifactStorage | None" = None,
    turn_no: int | None = None,
    tool_calls_as_claims_enabled: bool = False,
    resolve_scoped_call: "Callable[[str], tuple[str, int]] | None" = None,
    await_dispatch_barrier: "Callable[[], Awaitable[None]] | None" = None,
    tools: "list[Tool] | None" = None,
    budget: "PromptGuard | None" = None,
    initial_tool_round: int = 0,
    interrupt: "asyncio.Event | None" = None,
    interrupted_out: "list[bool] | None" = None,
    capped_out: "list[bool] | None" = None,
    stopped_park_out: "list[YieldToWorker | ToolWaitPark] | None" = None,
    stopped_calls_out: "list[ToolCallPart] | None" = None,
    intercept_context_overflow: bool = False,
) -> AsyncIterator[StreamEvent]:
    """Run one full agent turn with tool dispatch; stream events live.

    Parameters
    ----------
    agent
        The agent definition (used for ``temperature``).
    llm, llm_model
        LLM client + model resolved by the caller.
    tool_manager
        Source of the tool catalogue + dispatch surface. Pass an
        empty :class:`ToolExecutionManager` if the agent should run
        without tools.
    prompt
        The full prompt at turn start: typically
        ``[system?, *history, *new_user_messages]``.
    response_format
        Optional JSON Schema (or Pydantic class) forwarded to
        ``llm.stream``.
    principal
        Forwarded to every :meth:`ToolExecutionManager.execute` call
        for OAuth-aware MCP toolsets.
    messages_out
        Optional caller-provided list. The helper appends every
        message produced during the turn (assistant message + tool-
        result messages) to it, in order.
    artifact_storage
        When given, every part with an ``artifact_id`` (image/document
        attachments, MCP tool-result media) is resolved to inline
        ``data`` immediately before each ``llm.stream`` call -- an
        adapter only ever reads ``data``/``url``/``file_id``, never
        ``artifact_id``. Re-run every tool round so media a tool
        produces mid-turn is hydrated too, not just the turn's
        starting prompt. ``None`` (the default) is a no-op: callers
        that never resolve a store keep today's behaviour exactly.
    turn_no
        The enclosing session turn's own turn number (01a0518b), read
        by the tool-dispatch seam to scope any ``ToolCallTask`` rows it
        creates when ``tool_calls_as_claims`` is enabled -- see
        ``_dispatch_tool_calls``. A NESTED subagent call
        (``system__invoke_agent``) threads the SAME value through
        unchanged rather than minting its own: it belongs to the outer
        turn, not a fresh one, and scoping it differently would orphan
        its own tool-call scoped ids from the record they need to pair
        against. ``None`` (the default) is a no-op for callers that
        haven't opted into the feature.
    tool_calls_as_claims_enabled
        Whether the tool-dispatch seam should mint independently-claimable
        ``ToolCallTask`` rows for this turn's batch instead of dispatching
        in-process (01a0518b). TOP-LEVEL ONLY, deliberately: unlike
        ``turn_no`` (pure bookkeeping, ambiguity-free to inherit), this
        flag ARMS machinery -- specifically a ``tool_wait`` park, which is
        SESSION-anchored (``parked_state``, the re-arm event, the resume
        coordinator all key off a session row). A nested subagent turn
        (``system__invoke_agent``) has no session row of its own to park
        on, so it is NEVER passed this flag -- ``run_subagent`` /
        ``resume_subagent`` don't even accept it as a parameter, the same
        deliberate scope-cut class as ``artifact_storage``'s own cut for
        that exact surface. A nested batch always dispatches in-process,
        regardless of the enclosing turn's own setting. ``False`` (the
        default) is today's in-process behaviour, unchanged.
    resolve_scoped_call
        Maps a call's RAW provider id (``ToolCallPart.id``, e.g.
        ``"call_0"`` -- the only id the dispatch layer has ever had) to
        ``(scoped_call_id, record_seq)`` -- the ``node:tool:turn_no:seq``
        id the durable ``TOOL_CALL`` record actually carries, and that
        record's own ``messages.jsonl`` seq (01a0518b). The tool-dispatch
        seam cannot construct a ``ToolCallTask`` (or park state
        referencing one) without it: the scoped id does not otherwise
        reach this layer at all (see the 01a0518b ground-truth remap),
        and it must be the SAME string the transcript's own TOOL_CALL
        record carries or the two are never joinable. A successful
        return is also the proof this call's record is durable
        (flushed, not just appended) -- see ``dispatch.py``'s
        ``except ToolWaitPark`` branch and the CoalesceState
        ``tool_call_record_seq`` map it reads from; a caller must never
        raise ``ToolWaitPark`` for a call this failed to resolve.
        A NON-``None`` resolver is required for the notifying/claimable
        split to ever fire, even when ``tool_calls_as_claims_enabled``
        is True -- see ``_dispatch_tool_calls``'s own gate; ``None``
        (the default) falls through to today's in-process behaviour
        unchanged. Nested subagent calls never receive one, matching
        the flag's own scope-cut. A PURE lookup -- no ordering
        side-effect belongs inside it (see ``await_dispatch_barrier``
        below for where a surface's own ordering concern goes instead).
    await_dispatch_barrier
        Optional, additive (01a0518b, graph-surface boundary): awaited
        ONCE at the top of ``_dispatch_as_claims``, before resolving any
        call in the batch. The chat/workspace surface is a single
        pull-chain (no concurrent producer -- events are consumed the
        instant they're yielded), so ``resolve_scoped_call`` alone is
        always safe there and this stays ``None``. The graph surface's
        live fan-out is NOT a pull-chain: concurrent sibling node tasks
        share one queue, so a node's own tool-dispatch code can race
        ahead of the drainer that populates the very state
        ``resolve_scoped_call`` reads. A graph node binds this to
        ``primer.graph._node_refs.await_tool_dispatch_barrier`` (already
        built for this purpose) so every event IT queued before dispatch
        ran is guaranteed drained first. Once per BATCH, not once per
        call: the whole batch was queued before dispatch runs, so one
        barrier covers every call in it, and it is a pure ordering
        concern -- not a durability proof, unlike ``resolve_scoped_call``
        -- so it must never be folded into that callable's own contract.

    tools
        The tool catalogue to offer the model, already fetched by the caller.
        ``None`` (the default) fetches it from ``tool_manager.list_tools`` at the
        top of the turn, exactly as before. A caller that needs the catalogue
        itself (to size the prompt before the turn starts) fetches it once and
        passes it here so the loop does not fetch it a second time.
    budget
        Optional :class:`PromptGuard`; see its docstring. ``None`` (the default)
        sends every prompt exactly as the loop built it.
    initial_tool_round
        Tool rounds this turn has already spent in earlier attempts (default 0).
        A replay after a context overflow starts with the rounds the rejected attempt
        completed already in its history, and passes how many, so
        ``agent.max_tool_turns`` bounds the TURN and not each attempt.
    interrupt
        Optional Stop signal (the session dispatch sets it). The loop races it
        against every wait for the model's next event, so a model that has not
        produced its first token is stoppable too (see
        :func:`primer.agent.interrupt.interruptible`). When it fires the turn
        ends CLEANLY (no exception), after the provider stream is closed.
        A call that is already RUNNING is stopped too (slice B1): it runs as its
        own task (:func:`primer.agent.stoppable_call.run_stoppable`) and an
        interruptible call is cancelled (its cleanup, an exec's process-group
        kill, runs before the answer), while one the tool declares not
        interruptible (a file write) is waited for, a few seconds, and keeps its
        real result. A call that finishes first, or in the same wake-up as the
        Stop, always keeps its real result; one with none to give is answered
        ``interrupted: stopped by user ...`` (yielded and appended, so the log
        stays paired), and the turn ends before the next model call.
        The calls of the same batch that have not STARTED do not run: the batch
        is run one call after another, and once the Stop is set each remaining
        call is answered with a synthetic ``not run: stopped by user`` error
        result in place of its real one. A Stop that has landed BEFORE a round's
        batch starts runs none of it, answered the same way (appended to
        ``messages_out`` and yielded), so the history stays valid for the next
        request. Only the call that is RUNNING when the Stop lands can ask to
        park (a timer yield, an approval or answer gate, or a ``tool_wait``
        park). A park that waits on no human decision is ended as a Stop: the
        loop catches the park exception while the event is set, answers the
        round (the calls that finished before the one that asked to wait keep
        their real results, which the dispatch stamps on the exception as it
        leaves; the call that asked to wait, found by its position in the batch
        and not by its id, says ``interrupted: stopped by user ...``; the later
        ones ``not run: stopped by user``; a ``tool_wait`` batch keeps its
        notifying calls' real results), appends to ``interrupted_out``, hands
        the park to ``stopped_park_out`` and returns. A park that asks a PERSON (see
        :data:`primer.model.yield_.YIELD_KIND_PREFIXES`) still parks and the
        Stop is dropped: what they answer later wins. Calls later in the batch
        are refused once the Stop is set, so they can neither park nor deliver
        a client action. A Cancel
        sets the same event and is treated the same way (the dispatch then ends
        the session instead of resting it). ``None`` (the default) changes
        nothing.
    interrupted_out
        Optional caller-provided list; ``True`` is appended when the turn ended
        because ``interrupt`` fired (the same output-parameter shape as
        ``messages_out``). ``messages_out`` then holds only COMPLETED rounds: the
        interrupted round's partial assistant text is never appended, so it never
        reaches the model's history.
    stopped_park_out
        Optional caller-provided list; the park exception is appended when a Stop
        ended a park (see ``interrupt``). The dispatch needs it: what the park's
        tool had already created (an external tool's pending call row) is cleaned
        up by the cancelled exit, which only the dispatch can reach.
    stopped_calls_out
        Optional caller-provided list; each tool call the Stop CANCELLED or
        ABANDONED (it had no result to give, so it was answered ``interrupted``)
        is appended. Like ``stopped_park_out`` the dispatch needs it: a call
        cancelled after its tool wrote something before it could yield (an
        external tool's pending call row) never reaches the yield, so the
        park-keyed cleanup alone would miss it.
    capped_out
        Optional caller-provided list; ``True`` is appended when the turn ended
        because the model asked for another tool round at ``agent.max_tool_turns``
        (the round that REACHES the cap is answered with ``not executed``
        results, not run). The model's last event was still ``Done(tool_use)``,
        so without this a caller cannot tell a cap trip from a turn that is
        mid-chain. A Stop that lands on the same round wins and is reported
        through ``interrupted_out`` only.
    intercept_context_overflow
        When True, a call whose stream is ONLY a fatal ``Error`` that classifies as a context
        overflow (:func:`primer.common.context_overflow.is_context_overflow_error`: Ollama and Gemini
        yield the provider's 400 instead of raising it) is not yielded: the ``llm_call`` telemetry
        still is (the call did fail), then :class:`~primer.model.chat.TurnStreamOverflow` is raised.
        "Only" means no assistant CONTENT: events that are not content (``Usage``, say) may come
        before the ``Error`` and have been yielded as usual. For a caller that recovers from an
        overflow: the yielded ``Error`` is a terminal record, and a turn that recovers must not carry
        one before its real end. A stream that streamed content
        before the error is not intercepted (that content is already out): it fails as a
        :class:`~primer.model.chat.TurnStreamFailure`, ``Error`` yielded, as before. ``False`` (the
        default) changes nothing.

    Raises
    ------
    primer.model.except_.AuthRequiredError
        Propagated from a tool dispatch -- callers handle this
        (chat: terminal stream Error; workspace: WAITING transition;
        graph: per-node FAILED).
    primer.model.chat.TurnStreamOverflow
        With ``intercept_context_overflow``: the stream was only a fatal context-overflow ``Error``
        (a :class:`~primer.model.chat.TurnStreamFailure` subclass, so a handler of those catches it).
    primer.model.chat.TurnStreamFailure
        The LLM stream ended in a terminal :class:`Error` for this call
        (e.g. a connect failure), whether or not it produced any
        assistant content first (01a070d6). Callers must not treat an
        unraised return from this generator as success without checking
        for this -- see each caller's own handling.
    """
    if tools is None:
        tools = await tool_manager.list_tools(principal=principal)

    tool_round = initial_tool_round
    while True:
        if interrupt is not None and interrupt.is_set():
            # A Stop that landed during the previous round's tool batch (or before the
            # turn began): end here, before spending a model call on it.
            if interrupted_out is not None:
                interrupted_out.append(True)
            return
        if artifact_storage is not None:
            prompt = await hydrate_prompt_parts(artifact_storage, prompt)
        if budget is not None:
            # The loop's own ``prompt`` stays the unreduced accumulation: a guard
            # that prunes must re-derive (or re-apply a recorded set) on every
            # call, never rely on this variable carrying its previous output.
            send_prompt = await budget.before_call(prompt, tools=tools)
            # Unreduced messages keep their identity through a guard, so "sent unchanged" is decidable.
            guard_state = (
                "kept" if len(send_prompt) == len(prompt) and all(a is b for a, b in zip(send_prompt, prompt))
                else "reduced"
            )
        else:
            send_prompt = prompt
            guard_state = "none"
        buffered: list[StreamEvent] = []
        held_done: StreamEvent | None = None
        call_t0 = time.monotonic()
        call_usage: Usage | None = None
        stream = llm.stream(
            model=llm_model.model_name,
            messages=send_prompt,
            temperature=agent.temperature,
            max_output_tokens=agent.max_output_tokens,
            response_format=response_format,
            tools=tools,
            tool_choice="auto",
        )
        stream_it = stream.__aiter__()
        try:
            while True:
                try:
                    # The scope wraps ONLY the await, never the yield below: it cancels the
                    # task that entered it (see primer.agent.interrupt).
                    async with interruptible(interrupt):
                        event = await stream_it.__anext__()
                except StopAsyncIteration:
                    break
                except Interrupted:
                    if held_done is None:
                        raise
                    # The terminal event is already in, so the round is COMPLETE and only the
                    # end of the stream is left to drain. A Stop here must not discard it
                    # (that would drop a tool call the model had finished asking for), and a
                    # provider that never closes the stream must not hold the Stop for the
                    # stall timeout: close it and treat it as the end of the stream. The
                    # completed round is KEPT, but its tool calls are not run: the Stop is
                    # already set, so the check after the stream answers each call
                    # ``not run: stopped by user`` and ends the turn before the next model call.
                    aclose = getattr(stream_it, "aclose", None)
                    if aclose is not None:
                        with contextlib.suppress(Exception):
                            await aclose()
                    break
                buffered.append(event)
                if isinstance(event, Usage):
                    call_usage = event
                if isinstance(event, (Done, Error)) and held_done is None:
                    # Held so the llm_call event below reaches consumers
                    # FIRST: the record it becomes must land inside this
                    # turn's seq window, and a DONE record closes that
                    # window (primer/session/timeline.py). Error is a
                    # terminal too - letting it through closed the window
                    # before the telemetry landed, so every errored
                    # turn's trace came up empty (live finding
                    # 2026-08-25). ``buffered`` keeps the original order
                    # for output_to_message.
                    held_done = event
                    continue
                yield event
        except Interrupted:
            # Stop landed while waiting for the model (its first event, or between
            # chunks). Counted like any call (a stream that was cut short is
            # not an "ok" and not lost), but no ``llm_call`` record is produced:
            # like a stream that raises, an interrupted one has no Done to precede.
            elapsed = _observe_llm_call(llm_model, call_t0, call_usage, "interrupted")
            await _emit_llm_called(
                tool_manager, llm_model, call_usage, elapsed, "interrupted",
            )
            aclose = getattr(stream_it, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
            if interrupted_out is not None:
                interrupted_out.append(True)
            return
        except Exception:
            err_elapsed = _observe_llm_call(
                llm_model, call_t0, call_usage, "error",
            )
            await _emit_llm_called(
                tool_manager, llm_model, call_usage, err_elapsed, "error",
            )
            raise
        call_status = "error" if isinstance(held_done, Error) else "ok"
        try:
            assistant_msg = output_to_message(buffered)
            no_content: ValueError | None = None
        except ValueError as exc:
            assistant_msg, no_content = None, exc
        # An error-only overflow stream, for a caller that recovers: the Error is held back, not yielded.
        intercepted = (
            intercept_context_overflow
            and no_content is not None
            and isinstance(held_done, Error)
            and is_context_overflow_error(held_done)
        )
        if budget is not None:
            budget.after_call(call_usage)
        elapsed = _observe_llm_call(llm_model, call_t0, call_usage, call_status)
        estimated_input = _observe_prompt_estimate(llm_model, call_usage, send_prompt, tools)
        await _emit_llm_called(
            tool_manager, llm_model, call_usage, elapsed, call_status,
        )
        yield ExtendedEvent(
            extended=_LlmCall(
                profile_id=llm_model.profile_id,
                provider_id=llm_model.provider_id,
                model=llm_model.model_name,
                input_tokens=call_usage.input_tokens if call_usage else None,
                output_tokens=call_usage.output_tokens if call_usage else None,
                estimated_input_tokens=estimated_input,
                cached_input_tokens=call_usage.cached_input_tokens if call_usage else None,
                context_length=llm_model.context_length if estimated_input is not None else None,
                guard=guard_state,
                duration_ms=max(0, int(elapsed * 1000)),
                status=call_status,
            )
        )
        # Decided BEFORE the round's ``Done`` is delivered, so the durable ``done`` record of the round that trips the
        # cap says so (the model's own ``tool_use`` stays in ``raw_reason``) instead of reading as a mid-chain tool
        # round. The order is the one the checks below use: a Stop that already landed beats the cap (the ``llm_call``
        # event above is the last suspension point a Stop can land in before this decision). The capped round's ``Done``
        # is delivered AFTER the refusal results (the cap branch below): the ``done`` closes the turn's window
        # (``closes_turn``), and the results belong inside it.
        will_cap = (
            not intercepted
            and no_content is None
            and assistant_msg is not None
            and not isinstance(held_done, Error)
            and any(isinstance(p, ToolCallPart) for p in assistant_msg.parts)
            and not (interrupt is not None and interrupt.is_set())
            and agent.max_tool_turns is not None
            and tool_round + 1 >= agent.max_tool_turns
        )
        capped_done: Done | None = None
        if will_cap and isinstance(held_done, Done):
            capped_done = held_done.model_copy(update={"stop_reason": "tool_turn_cap"})
        if held_done is not None and not intercepted and capped_done is None:
            yield held_done

        if no_content is not None:
            exc = no_content
            if intercepted:
                raise TurnStreamOverflow(
                    held_done,
                    partial_messages=[],
                    rounds_completed=tool_round,
                ) from exc
            if isinstance(held_done, Error):
                # 01a070d6: an error-only stream (e.g. an LLM connect
                # failure) used to end here quietly - the ERROR record
                # already yielded above is durable, but nothing told the
                # caller the TURN failed, so every caller read this as an
                # ordinary empty completion.
                raise TurnStreamFailure(
                    held_done,
                    partial_messages=[],
                    rounds_completed=tool_round,
                ) from exc
            # Empty stream, no error: the LLM legitimately produced
            # nothing. The events were already emitted to subscribers, so
            # the user sees something, but the orchestrator would
            # otherwise treat the turn as a quiet success and tight-loop
            # the LLM. Log enough to make the situation diagnosable from
            # production logs.
            logger.warning(
                "agent loop: LLM stream produced no assistant message; "
                "ending turn without persisting (event_count=%d, error=%s)",
                len(buffered), exc,
            )
            return

        if messages_out is not None:
            messages_out.append(assistant_msg)

        if isinstance(held_done, Error):
            # 01a070d6: partial-content-then-error. output_to_message only
            # raises on ZERO convertible content, so any TextDelta before
            # the terminal error lets this succeed with a truncated-but-
            # normal-looking assistant message - worse than the empty
            # case, since it reads as a complete answer rather than an
            # obviously-empty one. The ERROR record is already durable;
            # this is what tells the turn itself it failed.
            raise TurnStreamFailure(
                held_done,
                partial_messages=[assistant_msg],
                rounds_completed=tool_round,
            )

        tool_calls = [
            p for p in assistant_msg.parts if isinstance(p, ToolCallPart)
        ]
        if not tool_calls:
            return

        # ``and not will_cap`` is DEFENSIVE ONLY and is not covered by a test: nothing suspends between the decision
        # above and this check (the capped ``Done`` is delivered from inside the cap branch, after it), so a Stop
        # cannot land in between and the guard never changes the outcome. It keeps the record (``tool_turn_cap``) and
        # ``capped_out`` / ``interrupted_out`` in agreement if a suspension point is ever added there.
        if interrupt is not None and interrupt.is_set() and not will_cap:
            # A Stop that landed as the model finished (or while its terminal event was draining). The
            # model already asked for these calls, but the user has since said stop: running a
            # destructive one now would make Stop a lie. None of the round runs; each call is answered so
            # the history stays valid for the next request. A call that has already STARTED is another
            # matter (see ``_dispatch_tool_calls``): this only covers a Stop that lands before the batch begins.
            for answer_event in _answer_undispatched(tool_calls, _STOPPED_REFUSAL, messages_out):
                yield answer_event
            if interrupted_out is not None:
                interrupted_out.append(True)
            return

        tool_round += 1
        if will_cap:
            # The assistant keeps emitting tool calls. Force-stop the turn
            # before dispatching another round so a model that never stops
            # cannot spend tokens / loop unbounded.
            logger.warning(
                "agent loop: reached max_tool_turns cap; force-stopping turn "
                "(agent_id=%s, max_tool_turns=%s, tool_round=%d)",
                getattr(agent, "id", None), agent.max_tool_turns, tool_round,
            )
            # The assistant message with these tool calls is already in messages_out and
            # will be persisted. Left unanswered it makes every later request invalid
            # (Anthropic and OpenAI chat both reject a tool_use / tool_calls with no
            # tool_result after it) and nothing recovers it. Answer each call with an
            # error result instead of dropping it: the model is told its call was not
            # run, and the same result is yielded so the durable log stays paired too.
            for answer_event in _answer_undispatched(tool_calls, _TOOL_CAP_REFUSAL, messages_out):
                yield answer_event
            if capped_out is not None:
                capped_out.append(True)
            if capped_done is not None:
                yield capped_done
            return

        client_actions: list[_ClientAction] = []
        try:
            tool_result_msgs = await _dispatch_tool_calls(
                tool_calls,
                tool_manager=tool_manager,
                principal=principal,
                actions_out=client_actions,
                tool_calls_as_claims_enabled=tool_calls_as_claims_enabled,
                resolve_scoped_call=resolve_scoped_call,
                await_dispatch_barrier=await_dispatch_barrier,
                interrupt=interrupt,
                stopped_calls_out=stopped_calls_out,
            )
        except (YieldToWorker, ToolWaitPark) as park:
            # A Stop that landed while the call that asks to wait was running (it can raise its park in the same
            # wake-up as the Stop, before a cancel reaches it) would be dropped by parking: the console then
            # offers no Stop, and the timer later runs the work
            # the user tried to stop. A park that waits on no human decision ends the turn as a Stop instead: the
            # round is answered, so the history stays valid, and the caller sees the interruption exactly as for
            # a Stop before the batch. A park that asks a person still parks (what they answer later wins).
            if (
                interrupt is None
                or not interrupt.is_set()
                or (isinstance(park, YieldToWorker) and asks_a_person(park.yielded))
            ):
                raise
            for answer_event in _answer_a_stopped_park(park, tool_calls, client_actions, messages_out):
                yield answer_event
            if interrupted_out is not None:
                interrupted_out.append(True)
            if stopped_park_out is not None:
                stopped_park_out.append(park)
            return
        # Delivery frames go out BEFORE the results so the session log
        # reads tool_call -> client_action -> tool_result, matching the
        # notifying contract (deliver, then answer).
        for action in client_actions:
            yield ExtendedEvent(extended=action)
        for trm in tool_result_msgs:
            if messages_out is not None:
                messages_out.append(trm)
            for part in trm.parts:
                if isinstance(part, ToolResultPart):
                    synth = ExtendedEvent(
                        extended=_ExecutorToolResult(
                            call_id=part.id,
                            output=part.output,
                            error=part.error,
                            metadata=part.metadata,
                        )
                    )
                    yield synth

        prompt = prompt + [assistant_msg, *tool_result_msgs]


def _partition_notifying(
    calls: list[ToolCallPart], tool_manager: ToolExecutionManager,
) -> tuple[list[ToolCallPart], list[ToolCallPart]]:
    """Split a tool-call batch into ``(notifying, claimable)``.

    Each subset keeps its calls in their original relative order from
    ``calls``.

    01a0518b: the eventual tool-dispatch seam needs exactly this
    partition to know which calls in a batch it may ever schedule as
    independent ``ToolCallTask`` rows (``claimable``) versus which it
    must always answer inline, regardless of ``tool_calls_as_claims`` -
    a notifying call (S3 spec section 3: the runner answers it itself
    with a synthetic success, the park machinery is never entered) has
    nothing to park ON in the first place, so routing it through the
    claim machinery would be pure overhead for zero behavioural
    difference. Extracted as its own pure function, ahead of
    ``_dispatch_tool_calls`` actually consuming it, so the partition
    logic is independently testable before the dispatch loop itself is
    rewritten to branch on it.
    """
    notifying: list[ToolCallPart] = []
    claimable: list[ToolCallPart] = []
    for call in calls:
        if tool_manager.is_notifying(call.name):
            notifying.append(call)
        else:
            claimable.append(call)
    return notifying, claimable


async def _dispatch_tool_calls(
    calls: list[ToolCallPart],
    *,
    tool_manager: ToolExecutionManager,
    principal: str | None,
    actions_out: list[_ClientAction],
    tool_calls_as_claims_enabled: bool = False,
    resolve_scoped_call: "Callable[[str], tuple[str, int]] | None" = None,
    await_dispatch_barrier: "Callable[[], Awaitable[None]] | None" = None,
    interrupt: "asyncio.Event | None" = None,
    stopped_calls_out: "list[ToolCallPart] | None" = None,
) -> list[Message]:
    """Dispatch tool calls; return tool-role messages to feed back to the LLM.

    AuthRequiredError propagates so the caller can react. All other
    :class:`PrimerError` instances are converted to
    ``ToolResultPart(error=True)`` by the manager itself; the
    defensive catch here is belt-and-braces for adapter bugs.

    In the in-process loop the calls run one after another, and ``interrupt`` (the Stop
    signal) is checked before each one: once the Stop is set every call that has NOT
    started is answered ``not run: stopped by user`` instead of running, so a Stop pressed
    during call 1 does not see calls 2..N execute. The batch is still answered in full, in
    order, so the history stays paired. A call that is RUNNING when the Stop lands is
    handled by :func:`primer.agent.stoppable_call.run_stoppable` (slice B1): it is
    cancelled, or waited for if its tool is not interruptible, or it keeps its real result
    if it finishes first; a call with no result to give is answered ``interrupted: stopped
    by user ...``. The claims path
    (:func:`_dispatch_as_claims`) parks the batch and is not covered by this per-call check. A park raised by the
    call that was RUNNING when the Stop landed leaves through the exception: ``run_agent_turn`` ends it as a Stop
    (see its ``interrupt`` parameter).

    When ``tool_calls_as_claims_enabled`` and the batch has at least one
    CLAIMABLE call (see :func:`_partition_notifying`), routes to
    :func:`_dispatch_as_claims` instead, which never returns normally --
    it raises :class:`~primer.model.yield_.ToolWaitPark`. Any other
    combination (flag off, or a batch that turns out to be entirely
    notifying once partitioned) falls through to today's in-process
    loop unchanged.
    """
    if tool_calls_as_claims_enabled and resolve_scoped_call is not None:
        notifying_calls, claimable_calls = _partition_notifying(calls, tool_manager)
        if claimable_calls:
            return await _dispatch_as_claims(
                notifying_calls,
                claimable_calls,
                tool_manager=tool_manager,
                principal=principal,
                actions_out=actions_out,
                resolve_scoped_call=resolve_scoped_call,
                await_dispatch_barrier=await_dispatch_barrier,
            )

    result_parts: list[ToolResultPart] = []
    for index, call in enumerate(calls):
        if interrupt is not None and interrupt.is_set():
            result_parts.extend(
                ToolResultPart(id=skipped.id, output=_STOPPED_REFUSAL, error=True) for skipped in calls[index:]
            )
            break
        # See _partition_notifying's docstring for why a notifying call
        # (checked the same way here) can never become a ToolCallTask row.
        if tool_manager.is_notifying(call.name):
            actions_out.append(
                _ClientAction(
                    call_id=call.id,
                    name=call.name,
                    arguments=dict(call.arguments or {}),
                )
            )
            # Notifying class (S3 spec section 3): the runner answers the
            # call itself with a successful synthetic tool_result and keeps
            # looping. The park machinery is never entered.
            result_parts.append(
                await tool_manager.deliver_notifying(call, principal=principal)
            )
            continue
        try:
            if interrupt is None:
                rp = await tool_manager.execute(call, principal=principal)
            else:
                # Its own task, raced against the Stop (see primer.agent.stoppable_call): an interruptible call is
                # cancelled, one that is not is waited for, and a call that finishes keeps its real result. None
                # means the Stop fired and the call has none to give: the same "interrupted" answer a Stop at a
                # park gets. A call it gives up on is abandoned through its CallScope (what it and every subagent it
                # started still emit is dropped by the delegation recorder), on the Stop path and on a hard Cancel.
                result = await run_stoppable(
                    lambda: tool_manager.execute(call, principal=principal),
                    interrupt=interrupt,
                    interruptible=lambda: tool_manager.is_interruptible_call(call),
                    name=call.name,
                )
                if result is None:
                    if stopped_calls_out is not None:
                        stopped_calls_out.append(call)
                    result = ToolResultPart(id=call.id, output=_PARK_STOPPED_REFUSAL, error=True)
                rp = result
        except AuthRequiredError:
            raise
        except YieldToWorker as park:
            # Say where in this batch the call asked to wait and what had finished before it, as the exception
            # leaves (a Stop that ends the park answers the round from these; a normal park ignores them). The
            # outermost batch stamps last, so a nested yield carries the OUTER position, not the subagent's.
            park.batch_index = index
            park.completed_results = list(result_parts)
            raise
        except PrimerError as exc:  # defence-in-depth.
            rp = ToolResultPart(id=call.id, output=str(exc), error=True)
        result_parts.append(rp)
    if not result_parts:
        return []
    return [Message(role="tool", parts=list(result_parts))]


async def _dispatch_as_claims(
    notifying_calls: list[ToolCallPart],
    claimable_calls: list[ToolCallPart],
    *,
    tool_manager: ToolExecutionManager,
    principal: str | None,
    actions_out: list[_ClientAction],
    resolve_scoped_call: "Callable[[str], tuple[str, int]]",
    await_dispatch_barrier: "Callable[[], Awaitable[None]] | None" = None,
) -> list[Message]:
    """Answer notifying calls inline, then park the claimable batch.

    Never returns normally -- always raises
    :class:`~primer.model.yield_.ToolWaitPark`. ``notifying_calls`` are
    answered synchronously first (S3 spec section 3, unchanged from the
    in-process path: they have nothing to park on), but since raising
    discards this function's own return value, their results ride on
    the exception's ``notifying_results`` field rather than a normal
    ``[Message]`` return -- see ``ToolWaitPark``'s own docstring for why
    that keeps "every sibling ``ToolCallTask`` row" the single
    reassembly truth instead of a second, parallel one.

    01a0518b: ``resolve_scoped_call`` is called for EVERY call in this
    batch, notifying and claimable alike -- not just to learn the
    scoped id, but because a successful return is itself the "this
    call's TOOL_CALL record is durable" proof (see
    ``run_agent_turn``'s own docstring on the parameter). This function
    intentionally does nothing with the ``record_seq`` half of the
    pair beyond that check: the ``except ToolWaitPark`` handler that
    actually constructs ``ToolCallTask`` rows already has direct
    ``_CoalesceState`` access and re-derives it there, so nothing here
    needs to carry it across the exception boundary too.

    ``await_dispatch_barrier`` (graph surface only) is awaited ONCE
    here, before resolving ANY call in the batch -- notifying and
    claimable alike need the drainer caught up, and the whole batch was
    already queued before this function ever runs, so one await covers
    it (see ``run_agent_turn``'s own docstring for the full reasoning
    on why this is separate from ``resolve_scoped_call``'s pure-lookup
    contract).
    """
    if await_dispatch_barrier is not None:
        await await_dispatch_barrier()

    notifying_results: list[tuple[str, ToolResultPart]] = []
    call_ids: dict[str, str] = {}
    for call in notifying_calls:
        actions_out.append(
            _ClientAction(
                call_id=call.id,
                name=call.name,
                arguments=dict(call.arguments or {}),
            )
        )
        rp = await tool_manager.deliver_notifying(call, principal=principal)
        scoped_id, _record_seq = resolve_scoped_call(call.id)
        notifying_results.append((scoped_id, rp))
        call_ids[scoped_id] = call.id

    outstanding_task_ids: list[str] = []
    for call in claimable_calls:
        scoped_id, _record_seq = resolve_scoped_call(call.id)
        outstanding_task_ids.append(scoped_id)
        call_ids[scoped_id] = call.id

    # Synthetic, non-pub/sub identifier (ToolWaitPark's own docstring) --
    # keyed on the first outstanding task so it is at least deterministic
    # and traceable back to this batch, not that anything ever looks it
    # up by value.
    event_key = f"tool_wait:{outstanding_task_ids[0]}"

    raise ToolWaitPark(
        outstanding_task_ids=outstanding_task_ids,
        event_key=event_key,
        notifying_results=notifying_results,
        call_ids=call_ids,
    )


__all__ = ["run_agent_turn"]
