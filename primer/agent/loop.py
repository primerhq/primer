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

import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import primer.observability.metrics as _metrics

from primer.agent.tool_manager import ToolExecutionManager
from primer.media.hydrate import hydrate_prompt_parts
from primer.model.chat import (
    Done,
    Error,
    ExtendedEvent,
    Message,
    StreamEvent,
    ToolCallPart,
    ToolResultPart,
    Usage,
    output_to_message,
    _ClientAction,
    _ExecutorToolResult,
    _LlmCall,
)
from primer.model.except_ import AuthRequiredError, PrimerError
from primer.model.yield_ import ToolWaitPark


if TYPE_CHECKING:
    from primer.int.artifact_storage import ArtifactStorage
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model_profile import ResolvedModel


logger = logging.getLogger(__name__)


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
    last_input_tokens_out: list[int | None] | None = None,
    artifact_storage: "ArtifactStorage | None" = None,
    turn_no: int | None = None,
    tool_calls_as_claims_enabled: bool = False,
    resolve_scoped_call: "Callable[[str], tuple[str, int]] | None" = None,
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
    last_input_tokens_out
        Optional caller-provided single-element list. The helper
        sets ``[0]`` to the most recent ``Usage.input_tokens`` value
        observed during the turn (or leaves it as-is if the LLM
        never emitted Usage).
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
        the flag's own scope-cut.

    Raises
    ------
    primer.model.except_.AuthRequiredError
        Propagated from a tool dispatch -- callers handle this
        (chat: terminal stream Error; workspace: WAITING transition;
        graph: per-node FAILED).
    """
    tools = await tool_manager.list_tools(principal=principal)

    tool_round = 0
    while True:
        if artifact_storage is not None:
            prompt = await hydrate_prompt_parts(artifact_storage, prompt)
        buffered: list[StreamEvent] = []
        held_done: StreamEvent | None = None
        call_t0 = time.monotonic()
        call_usage: Usage | None = None
        stream = llm.stream(
            model=llm_model.model_name,
            messages=prompt,
            temperature=agent.temperature,
            max_output_tokens=agent.max_output_tokens,
            response_format=response_format,
            tools=tools,
            tool_choice="auto",
        )
        try:
            async for event in stream:
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
                if (
                    last_input_tokens_out is not None
                    and isinstance(event, Usage)
                ):
                    if not last_input_tokens_out:
                        last_input_tokens_out.append(event.input_tokens)
                    else:
                        last_input_tokens_out[0] = event.input_tokens
        except Exception:
            err_elapsed = _observe_llm_call(
                llm_model, call_t0, call_usage, "error",
            )
            await _emit_llm_called(
                tool_manager, llm_model, call_usage, err_elapsed, "error",
            )
            raise
        call_status = "error" if isinstance(held_done, Error) else "ok"
        elapsed = _observe_llm_call(llm_model, call_t0, call_usage, call_status)
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
                duration_ms=max(0, int(elapsed * 1000)),
                status=call_status,
            )
        )
        if held_done is not None:
            yield held_done

        try:
            assistant_msg = output_to_message(buffered)
        except ValueError as exc:
            # Empty / error-only stream. The events were already emitted to
            # subscribers, so the user sees something, but the orchestrator
            # would otherwise treat the turn as a quiet success and tight-loop
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

        tool_calls = [
            p for p in assistant_msg.parts if isinstance(p, ToolCallPart)
        ]
        if not tool_calls:
            return

        tool_round += 1
        if (
            agent.max_tool_turns is not None
            and tool_round >= agent.max_tool_turns
        ):
            # The assistant keeps emitting tool calls. Force-stop the turn
            # before dispatching another round so a model that never stops
            # cannot spend tokens / loop unbounded.
            logger.warning(
                "agent loop: reached max_tool_turns cap; force-stopping turn "
                "(agent_id=%s, max_tool_turns=%s, tool_round=%d)",
                getattr(agent, "id", None), agent.max_tool_turns, tool_round,
            )
            return

        client_actions: list[_ClientAction] = []
        tool_result_msgs = await _dispatch_tool_calls(
            tool_calls,
            tool_manager=tool_manager,
            principal=principal,
            actions_out=client_actions,
            tool_calls_as_claims_enabled=tool_calls_as_claims_enabled,
            resolve_scoped_call=resolve_scoped_call,
        )
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
) -> list[Message]:
    """Dispatch tool calls; return tool-role messages to feed back to the LLM.

    AuthRequiredError propagates so the caller can react. All other
    :class:`PrimerError` instances are converted to
    ``ToolResultPart(error=True)`` by the manager itself; the
    defensive catch here is belt-and-braces for adapter bugs.

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
            )

    result_parts: list[ToolResultPart] = []
    for call in calls:
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
            rp = await tool_manager.execute(call, principal=principal)
        except AuthRequiredError:
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
    """
    notifying_results: list[tuple[str, ToolResultPart]] = []
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

    outstanding_task_ids: list[str] = []
    for call in claimable_calls:
        scoped_id, _record_seq = resolve_scoped_call(call.id)
        outstanding_task_ids.append(scoped_id)

    # Synthetic, non-pub/sub identifier (ToolWaitPark's own docstring) --
    # keyed on the first outstanding task so it is at least deterministic
    # and traceable back to this batch, not that anything ever looks it
    # up by value.
    event_key = f"tool_wait:{outstanding_task_ids[0]}"

    raise ToolWaitPark(
        outstanding_task_ids=outstanding_task_ids,
        event_key=event_key,
        notifying_results=notifying_results,
    )


__all__ = ["run_agent_turn"]
