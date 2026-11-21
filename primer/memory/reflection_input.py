"""Bounded, ephemeral reflection evidence. Estimates are never native token counts.

All textual fragments reach a call or the entire attempt refuses before learning.
No conversation compaction markers, tools, persisted summaries or recursive folds.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from itertools import chain
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from primer.common.context_overflow import is_context_overflow, is_context_overflow_error
from primer.int.llm import _extract_json_object
from primer.llm._reasoning import profile_extended
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback, text_tokens
from primer.memory.prompts import MEMORY_SUMMARY_PROMPT
from primer.model.chat import (
    Done, Error, ExtendedEvent, Message, TextDelta, TextPart,
    ToolCallDelta, ToolCallEnd, ToolCallPart, ToolCallStart, ToolResultPart,
)
from primer.model.except_ import ServerError

INPUT_TOKEN_CAP = 30_000
MAX_CHUNKS = 4
ATTEMPT_SECONDS = 120
MAX_OUTPUT_BYTES = 65_536
MAX_EVENTS = 20_000
FRAGMENT_CHARS = 1024


class InputRefused(Exception):
    """A fixed, non-sensitive reason; the source watermark must not advance."""


class InvalidVerdict(Exception):
    """The final model output is invalid (the existing terminal verdict policy)."""


class SummaryFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    observation: Annotated[str, Field(min_length=1, max_length=600)]
    sources: Annotated[list[Annotated[str, Field(max_length=100)]], Field(min_length=1, max_length=8)]


class EvidenceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    facts: Annotated[list[SummaryFact], Field(max_length=12)]


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def request_size(system: str, user: str, schema: dict) -> int:
    """Count all requested text, framing and JSON schema with the shared heuristic."""
    return count_tokens_char_fallback(messages=[
        Message(role="system", parts=[TextPart(text=system)]),
        Message(role="user", parts=[TextPart(text=user)]),
    ]) + text_tokens(encode(schema)) + 64


@dataclass(frozen=True)
class Budget:
    input_tokens: int
    output_tokens: int

    @classmethod
    def for_model(cls, window: int, *, summary: bool = False) -> Budget:
        output = min(1024 if summary else 4096, max(1, window // 4))
        return cls(min(INPUT_TOKEN_CAP, int(.8 * (window - output))), output)

    def fits(self, system: str, user: str, schema: dict) -> bool:
        return request_size(system, user, schema) <= self.input_tokens


@dataclass(frozen=True)
class Fragment:
    id: str
    line: str


def fragments(messages: list[Message], first_turn: int) -> Iterator[Fragment]:
    turn = first_turn - 1
    previous_role = None
    for mi, message in enumerate(messages):
        if message.role == "user" and previous_role != "user":
            turn += 1
        previous_role = message.role
        for pi, part in enumerate(message.parts):
            meta = {"turn": turn, "role": message.role}
            if isinstance(part, TextPart):
                value = part.text
            elif isinstance(part, ToolCallPart):
                meta.update(tool=part.name, call_id=part.id)
                value = encode(part.arguments)
            elif isinstance(part, ToolResultPart):
                meta.update(call_id=part.id, error=part.error)
                value = part.output
            else:
                value = f"[{part.type}: binary/nontext content not inspected]"
            for offset in range(0, max(1, len(value)), FRAGMENT_CHARS):
                piece = value[offset:offset + FRAGMENT_CHARS]
                ident = f"t{turn}:m{mi}:p{pi}:{offset}-{offset + len(piece)}"
                yield Fragment(ident, encode({"source": ident, **meta, "text": piece}))


def transcript(context: str, evidence: str) -> str:
    return context + "\n\n# Transcript\n" + evidence


def summary_input(chunk: list[Fragment]) -> str:
    return "# Memory evidence to summarize\n" + "\n".join(f.line for f in chunk)


@dataclass(frozen=True)
class Plan:
    direct: str | None
    chunks: tuple[list[Fragment], ...]
    summary_bytes: int = 0


def plan_input(messages: list[Message], first_turn: int, *, context: str, system: str, schema: dict, window: int) -> Plan:
    final = Budget.for_model(window)
    fixed = request_size(system, transcript(context, ""), schema)
    if fixed >= final.input_tokens:
        raise InputRefused("input_policy_too_large")
    summary_budget = Budget.for_model(window, summary=True)
    summary_schema = EvidenceSummary.model_json_schema()
    source = fragments(messages, first_turn)
    prefix: list[Fragment] = []
    for fragment in source:
        prefix.append(fragment)
        if not final.fits(system, transcript(context, "\n".join(f.line for f in prefix)), schema):
            break
    else:
        return Plan(transcript(context, "\n".join(f.line for f in prefix)), ())
    chunks: list[list[Fragment]] = []
    current: list[Fragment] = []
    # Retain only bounded chunks; refuse before any model request if the entire
    # range cannot be covered. The direct-path prefix is replayed, never lost.
    for fragment in chain(prefix, source):
        candidate = [*current, fragment]
        if not summary_budget.fits(MEMORY_SUMMARY_PROMPT, summary_input(candidate), summary_schema):
            if not current or len(chunks) >= MAX_CHUNKS - 1:
                raise InputRefused("input_compaction_limit")
            chunks.append(current)
            current = [fragment]
            if not summary_budget.fits(MEMORY_SUMMARY_PROMPT, summary_input(current), summary_schema):
                raise InputRefused("input_compaction_limit")
        else:
            current = candidate
    if current:
        chunks.append(current)
    if not chunks:
        raise InputRefused("input_compaction_limit")
    # A byte costs at most one unit in our text estimator. Leave additional room
    # for per-chunk labels and joining; verify the actual final request as well.
    output_bytes = min(4096, (final.input_tokens - fixed - 256) // len(chunks))
    if output_bytes < 256:
        raise InputRefused("input_policy_too_large")
    return Plan(None, tuple(chunks), output_bytes)


async def _close(stream) -> None:
    """Join provider cleanup even if the caller is cancelled again during close."""
    if not hasattr(stream, "aclose"):
        return
    task = asyncio.ensure_future(stream.aclose())
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            break  # retrieve below, without losing a cancellation already observed
    if cancelled is not None:
        if not task.cancelled():
            task.exception()
        raise cancelled
    task.result()


async def _judge_stream(llm, model, *, system: str, user: str, schema: dict,
                        budget: Budget, max_bytes: int = MAX_OUTPUT_BYTES) -> dict:
    if not budget.fits(system, user, schema):
        raise InputRefused("input_compaction_limit")
    # One adapter invocation, no retry of rejected profile knobs here. An
    # aggregate adapter can still have its own bounded member failover policy.
    stream = llm.stream(
        model=model.model_name or "", messages=[
            Message(role="system", parts=[TextPart(text=system)]),
            Message(role="user", parts=[TextPart(text=user)]),
        ], temperature=0.0, response_format=schema, max_output_tokens=budget.output_tokens,
        # No executable tools are exposed. Anthropic emulates the schema with
        # its reserved structured_output tool and forbids an explicit choice.
        tools=None, tool_choice=None,
        extended=profile_extended(llm, getattr(model, "config", None), model=model.model_name),
    )
    parts: list[str] = []
    size = events = received = 0
    structured_key = None
    structured_result = None
    argument_parts: list[str] = []
    argument_size = 0
    completed = False
    try:
        async for event in stream:
            events += 1
            # Reject large text before encoding another copy. Nontext provider
            # events are not retained; event count and the attempt deadline bound
            # streams with no text. This is not a network-frame-size guarantee.
            payload = event.extended if isinstance(event, ExtendedEvent) else event
            if "tool_call" in payload.type and not isinstance(event, (ToolCallStart, ToolCallDelta, ToolCallEnd)):
                raise InputRefused("compaction_invalid")
            text = getattr(payload, "text", None) or ""
            if len(text) > MAX_OUTPUT_BYTES:
                raise InputRefused("compaction_output_limit")
            received += len(text.encode("utf-8"))
            if received > MAX_OUTPUT_BYTES:
                raise InputRefused("compaction_output_limit")
            if events > MAX_EVENTS:
                raise InputRefused("compaction_output_limit")
            if isinstance(event, Done):
                if completed:
                    raise ServerError("memory model sent duplicate completion", code="network_error")
                if event.stop_reason == "max_tokens":
                    raise InputRefused("compaction_output_limit")
                if event.stop_reason == "error":
                    raise ServerError("memory model did not complete", code="network_error")
                if event.stop_reason not in ("stop", "tool_use"):
                    raise InputRefused("compaction_incomplete")
                if event.stop_reason == "tool_use" and structured_result is None:
                    raise InputRefused("compaction_incomplete")
                completed = True
                continue
            if isinstance(event, Error):
                # Nonfatal means another event may follow, not that a failed
                # response is valid evidence (notably OpenResponses failures).
                if is_context_overflow_error(event):
                    raise InputRefused("input_context_rejected")
                raise ServerError("memory model stream failed", code=event.code)
            if completed and (text or isinstance(event, (ToolCallStart, ToolCallDelta, ToolCallEnd))):
                raise ServerError("memory model sent content after completion", code="network_error")
            if isinstance(event, ToolCallStart):
                if event.name != "structured_output" or structured_key is not None:
                    raise InputRefused("compaction_invalid")
                structured_key = (event.id, event.index)
            elif isinstance(event, (ToolCallDelta, ToolCallEnd)):
                if structured_key != (event.id, event.index) or structured_result is not None:
                    raise InputRefused("compaction_invalid")
                if isinstance(event, ToolCallDelta):
                    if len(event.arguments_delta) > max_bytes:
                        raise InputRefused("compaction_output_limit")
                    amount = len(event.arguments_delta.encode("utf-8"))
                    argument_size += amount
                    received += amount
                    if argument_size > max_bytes or received > MAX_OUTPUT_BYTES:
                        raise InputRefused("compaction_output_limit")
                    argument_parts.append(event.arguments_delta)
                else:
                    # Bound an adapter's already-parsed result too, without
                    # first constructing one unbounded serialized copy.
                    end_size = 0
                    for piece in json.JSONEncoder(ensure_ascii=False).iterencode(event.arguments):
                        end_size += len(piece.encode("utf-8"))
                        if end_size > max_bytes:
                            raise InputRefused("compaction_output_limit")
                    if not argument_parts:
                        received += end_size
                        if received > MAX_OUTPUT_BYTES:
                            raise InputRefused("compaction_output_limit")
                    structured_result = event.arguments
            elif "tool_call" in payload.type:
                raise InputRefused("compaction_invalid")
            elif isinstance(event, TextDelta):
                size += len(event.text.encode("utf-8"))
                if size > max_bytes:
                    raise InputRefused("compaction_output_limit")
                parts.append(event.text)
    except Exception as exc:
        if is_context_overflow(exc):
            raise InputRefused("input_context_rejected") from None
        raise
    finally:
        await _close(stream)
    if not completed:
        raise ServerError("memory model stream ended without completion", code="network_error")
    try:
        if structured_key is not None:
            if structured_result is None:
                raise InputRefused("compaction_incomplete")
            if argument_parts and json.loads("".join(argument_parts)) != structured_result:
                raise InvalidVerdict()
            return structured_result
        return _extract_json_object("".join(parts))
    except (ValueError, RecursionError):
        raise InvalidVerdict() from None


async def bounded_judge(llm, model, **kwargs) -> dict:
    # Keep generator cleanup owned across repeated waiter cancellation. Cancelling
    # the waiter directly inside __anext__ could otherwise interrupt its finally.
    task = asyncio.create_task(_judge_stream(llm, model, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
            except Exception:
                break  # cleanup failed; original waiter cancellation still wins
        # Retrieve any cleanup failure but preserve the original cancellation.
        if not task.cancelled():
            task.exception()
        raise


async def prepare_input(plan: Plan, *, llm, model, context: str, system: str, schema: dict,
                        check: Callable[[], Awaitable[None]]) -> tuple[str, dict]:
    metadata = {"input_mode": "direct", "summary_calls": 0, "count_source": "estimate"}
    if plan.direct is not None:
        user = plan.direct
    else:
        evidence = []
        for index, chunk in enumerate(plan.chunks):
            await check()
            try:
                raw = await bounded_judge(
                    llm, model, system=MEMORY_SUMMARY_PROMPT, user=summary_input(chunk),
                    schema=EvidenceSummary.model_json_schema(), budget=Budget.for_model(model.context_length, summary=True),
                    max_bytes=plan.summary_bytes,
                )
                result = EvidenceSummary.model_validate(raw)
                allowed = {f.id for f in chunk}
                if any(ref not in allowed for fact in result.facts for ref in fact.sources):
                    raise InputRefused("compaction_invalid")
            except (InvalidVerdict, ValidationError):
                raise InputRefused("compaction_invalid") from None
            metadata["summary_calls"] += 1
            evidence.append(f"[lossy evidence summary {index + 1}; {chunk[0].id} through {chunk[-1].id}]\n" + encode(raw))
        metadata["input_mode"] = "compacted"
        user = transcript(context, "\n".join(evidence))
    budget = Budget.for_model(model.context_length)
    if not budget.fits(system, user, schema):
        raise InputRefused("input_compaction_limit")
    metadata.update(input_estimate=request_size(system, user, schema), input_budget=budget.input_tokens)
    return user, metadata
