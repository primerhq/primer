"""Translate the claude CLI's stream-json stdout into primer stream events.

The CLI (``--output-format stream-json --include-partial-messages``) emits one
JSON object per line. This module turns one step's lines into primer's
``StreamEvent``s per spec 6.5:

* ``stream_event`` / ``content_block_delta`` / ``text_delta`` -> ``TextDelta``
* ``thinking_delta`` -> ``ReasoningDelta`` (``signature_delta`` is ignored: the
  replay never needs the thinking signature)
* ``content_block_start`` with a ``tool_use`` block -> ``ToolCallStart`` under
  the UNALIASED scoped tool id (the model sees ``mcp__p__<alias>``); the one
  exception is the CLI's internal ``StructuredOutput`` tool (added by
  ``--json-schema`` even under ``--tools ""``, spec 6.7): it is the answer,
  not a primer tool call -- no events for it at all. The CLI validates its
  input against the schema and retries on a mismatch, so the ANSWER is the
  ``result`` line's ``structured_output``: a structured-only message's
  ``message_stop`` still yields ``Done`` (stop reason mapped to ``stop``, no
  kill) for the ADAPTER to hold back and read through to the result line
* ``input_json_delta`` -> ``ToolCallDelta``
* ``content_block_stop`` for a ``tool_use`` block -> ``ToolCallEnd`` with the
  parsed accumulated arguments; when the JSON never completed (cut off by
  ``max_tokens``) NO ``ToolCallEnd`` is emitted -- a garbage-arguments end must
  not reach primer
* ``message_start`` / ``message_delta`` usage -> ``Usage`` (``cumulative=False``,
  ``cached_input_tokens`` from ``cache_read_input_tokens``, reasoning tokens
  from ``output_tokens_details.thinking_tokens``)
* ``message_stop`` -> ``Done`` with the stop reason from the preceding
  ``message_delta`` (global-constraints stop map; ``raw_reason`` keeps the
  original)
* ``result`` with ``is_error`` -> ``Error(fatal=True)``; the same line mapped
  to an exception BEFORE the first event is :meth:`error_for_result`
* ``rate_limit_event`` -> no stream event, but its status is remembered for
  :meth:`error_for_result` (a plan limit)
* everything else (``system``, ``assistant``, ``user``, ``tool_progress``,
  hooks, ...) -> no events
"""

from __future__ import annotations

import json
from collections.abc import Callable

from primer.common.context_overflow import _message_says_overflow
from primer.model.chat import (
    Done,
    Error,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from primer.model.except_ import AuthenticationError, PrimerError, RateLimitError

#: The model-visible name prefix the CLI gives bridge tools (MCP server ``p``).
_MCP_PREFIX = "mcp__p__"

#: Global-constraints stop map; anything else maps to ``other``.
_STOP_MAP = {"end_turn": "stop", "tool_use": "tool_use", "max_tokens": "max_tokens"}

#: The name the CLI gives its internal structured-output tool (spec 6.7).
_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


def _stop_reason(raw: str | None) -> str:
    return _STOP_MAP.get(raw or "", "other")


def _usage_from(usage: dict) -> Usage:
    details = usage.get("output_tokens_details")
    thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
    # The whole prompt: the API's input_tokens excludes cache reads and cache
    # creation (total = input_tokens + cache_read + cache_creation); mirror
    # primer/llm/anthropic.py and sum them, or we undercount when caching is on.
    raw_in = int(usage.get("input_tokens") or 0)
    total_in = (
        raw_in
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )
    return Usage(
        input_tokens=total_in,
        output_tokens=int(usage.get("output_tokens") or 0),
        cached_input_tokens=usage.get("cache_read_input_tokens"),
        reasoning_tokens=thinking,
        cumulative=False,
    )


def _result_message(line: dict) -> str:
    """A human-readable message for a failing ``result`` line.

    The CLI's ``result`` text when it is one, else its ``errors`` list, else a
    diagnostic field (an interrupted run carries ``result: null``)."""
    result = line.get("result")
    if isinstance(result, str) and result:
        return result
    errors = line.get("errors")
    if isinstance(errors, list) and errors:
        return " ".join(str(e) for e in errors)
    for key in ("terminal_reason", "subtype"):
        value = line.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class StreamTranslator:
    """Stateful line-by-line translator for one CLI step's stdout.

    ``unalias`` turns a bridge tool name (the alias the CLI is shown) back
    into the scoped primer tool id; the ``mcp__p__`` prefix is stripped first.
    """

    def __init__(self, *, unalias: Callable[[str], str]) -> None:
        self._unalias = unalias
        # open tool_use blocks: block index -> (tool id, name)
        self._open_tools: dict[int, tuple[str, str]] = {}
        # open StructuredOutput blocks (spec 6.7): block index only
        self._open_structured: set[int] = set()
        # accumulated ``partial_json`` fragments per open block index
        self._fragments: dict[int, list[str]] = {}
        # raw stop reason of the latest message_delta (consumed by message_stop)
        self._last_stop: str | None = None
        # per-message facts, reset at message_start: whether this message opened a
        # StructuredOutput block and how many primer tool_use blocks it opened
        # (together they decide what a ``tool_use`` stop means)
        self._message_structured = False
        self._message_tool_uses = 0
        # status of the latest rate_limit_event (a plan limit, for error_for_result)
        self._last_rate_limit: str | None = None
        self._done = False
        self._kill_now = False

    @property
    def done(self) -> bool:
        """A terminal event (``Done`` or a failing-result ``Error``) was produced."""
        return self._done

    @property
    def kill_now(self) -> bool:
        """A ``message_stop`` with stop reason ``tool_use`` was seen: primer
        reads the pending call from the stream and kills the CLI process."""
        return self._kill_now

    def feed(self, line: dict) -> list[StreamEvent]:
        """Translate one stdout line; unknown line types produce no events."""
        line_type = line.get("type")
        if line_type == "stream_event":
            return self._stream_event(line.get("event") or {})
        if line_type == "rate_limit_event":
            info = line.get("rate_limit_info") or {}
            self._last_rate_limit = info.get("status")
            return []
        if line_type == "result":
            return self._result(line)
        return []  # system, assistant, user, tool_progress, control_response, ...

    def _stream_event(self, ev: dict) -> list[StreamEvent]:
        ev_type = ev.get("type")
        index = ev.get("index")

        if ev_type == "message_start":
            self._message_structured = False
            self._message_tool_uses = 0
            usage = (ev.get("message") or {}).get("usage")
            return [_usage_from(usage)] if usage else []

        if ev_type == "message_delta":
            events: list[StreamEvent] = []
            usage = ev.get("usage")
            if usage:
                events.append(_usage_from(usage))
            self._last_stop = (ev.get("delta") or {}).get("stop_reason")
            return events

        if ev_type == "message_stop":
            raw = self._last_stop
            if raw == "tool_use" and self._message_structured and not self._message_tool_uses:
                # Spec 6.7 v1.4: the message's only tool was the CLI's internal
                # StructuredOutput: it is the answer, not a primer call. No kill;
                # the adapter holds the Done and reads through to the result
                # line, whose structured_output is the validated answer; the
                # stop maps to ``stop`` (``raw_reason`` keeps the original).
                self._done = True
                return [Done(stop_reason="stop", raw_reason="tool_use")]
            if raw == "tool_use":
                self._kill_now = True
            self._done = True
            return [Done(stop_reason=_stop_reason(raw), raw_reason=raw or "")]

        if ev_type == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") != "tool_use" or index is None:
                return []
            name = block.get("name") or ""
            if name == _STRUCTURED_OUTPUT_TOOL:
                # Spec 6.7 v1.5: the CLI's internal tool, not a primer tool
                # call: no events for it; the answer is the result line's
                # structured_output, so its input is not even accumulated.
                self._open_structured.add(index)
                self._message_structured = True
                return []
            tool_id = block.get("id")
            self._open_tools[index] = (tool_id, name)
            self._fragments[index] = []
            self._message_tool_uses += 1
            return [ToolCallStart(
                id=tool_id,
                name=self._unalias(name.removeprefix(_MCP_PREFIX)),
                index=index,
            )]

        if ev_type == "content_block_delta":
            delta = ev.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                return [TextDelta(text=delta.get("text", ""), index=index)]
            if delta_type == "thinking_delta":
                return [ReasoningDelta(text=delta.get("thinking", ""), index=index)]
            if delta_type == "input_json_delta" and index in self._open_tools:
                partial = delta.get("partial_json", "")
                self._fragments[index].append(partial)
                return [ToolCallDelta(id=self._open_tools[index][0], arguments_delta=partial, index=index)]
            return []  # signature_delta and the StructuredOutput input: ignored (spec 6.7 v1.5)

        if ev_type == "content_block_stop":
            if index in self._open_structured:
                # Spec 6.7 v1.5: the StructuredOutput input is NOT the answer
                # (the CLI validates it and makes the model retry on a
                # mismatch): nothing is emitted and nothing is recorded.
                self._open_structured.discard(index)
                return []
            if index not in self._open_tools:
                return []
            tool_id, _name = self._open_tools.pop(index)
            raw = "".join(self._fragments.pop(index, []))
            try:
                args = json.loads(raw)
            except ValueError:
                return []  # truncated JSON (e.g. cut off by max_tokens): no end, no garbage
            if not isinstance(args, dict):
                return []
            return [ToolCallEnd(id=tool_id, arguments=args, index=index)]

        return []  # unknown stream event types: ignored

    def _result(self, line: dict) -> list[Error]:
        if not line.get("is_error"):
            return []
        self._done = True
        message = _result_message(line)
        # Spec 6.6 v1.5: a prompt the CLI reports as too long keeps the CLI
        # text under the bad_request code, so primer's context-overflow
        # handling (is_context_overflow*) recognises it on both channels.
        code = "bad_request" if _message_says_overflow(message) else "claude_code_result_error"
        return [Error(code=code, message=message, fatal=True)]

    def error_for_result(self, line: dict) -> PrimerError | None:
        """Map a failing ``result`` line to a primer exception, or ``None``.

        Called BEFORE the first stream event when the CLI fails fast (the first
        stdout line is a failing ``result``): a plan limit -- the CLI's
        ``rate_limit_event`` reported ``rejected`` -- is a :class:`RateLimitError`;
        a logged-out CLI is an :class:`AuthenticationError`; anything else is
        ``None`` (the adapter turns the terminal ``Error`` into its own failure
        path instead). The exact plan-limit wording is unverified, so the
        mapping keys on the last ``rate_limit_event`` status when one was seen,
        and on the result text otherwise.
        """
        if line.get("type") != "result" or not line.get("is_error"):
            return None
        result = line.get("result")
        text = result if isinstance(result, str) else ""
        if self._last_rate_limit == "rejected":
            return RateLimitError(text or "the plan's rate limit was rejected", code="claude_code_rate_limited")
        low = text.lower()
        if "logged in" in low or "log in" in low or "login" in low:
            return AuthenticationError(text or "not logged in", code="claude_code_not_authenticated")
        # Conservative fallback when no rate_limit_event was seen: a plan limit
        # phrased in the result text still maps to a rate-limit error.
        if "usage limit" in low or "rate limit" in low:
            return RateLimitError(text or "usage limit reached", code="claude_code_rate_limited")
        return None
