"""Per-chat turn runner — drives one user_message → assistant reply.

Wires a :class:`Chat` row through the LLM + tool stack the rest of
the framework already exposes. The runner:

1. Persists the user_message row.
2. Loads prior chat history (ChatMessage rows → :class:`Message` list,
   coalescing consecutive assistant_token rows into one assistant
   :class:`Message`).
3. Streams the LLM's response. Per stream event:

   * :class:`TextDelta` → persist an ``assistant_token`` ChatMessage
     row carrying the delta text. The UI renders these live.
   * :class:`ToolCallStart` → cache the tool name keyed by call id.
   * :class:`ToolCallEnd` → persist a ``tool_call`` ChatMessage row
     (name + parsed args + id), buffer for end-of-turn dispatch.
   * :class:`Done` → persist a ``done`` row and (if stop_reason ==
     ``tool_use``) dispatch each tool call via the
     :class:`ToolExecutionManager`, persist ``tool_result`` rows, and
     loop with the tool results appended to the prompt.
   * :class:`Error` → persist an ``error`` row and stop the turn.

The runner is constructed per WebSocket frame (cheap — holds storage
+ resolver handles only). All heavy resolution (LLM client, model
config, tool manager) happens once when the WS handler builds the
runner.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.agent.compaction import CompactionStrategy
from primer.agent.compaction_mixin import (
    apply_compaction as _mixin_apply_compaction,
    should_compact as _mixin_should_compact,
)
from primer.agent.prompts import DEFAULT_COMPACTION_PROMPT
from primer.int.storage import Storage
from primer.model.chat import (
    Done,
    Error,
    Message,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    Usage,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.yield_ import YieldToWorker
from primer.model.storage import (
    CursorPage,
    FieldRef,
    Op,
    OrderBy,
    Predicate,
    Value,
)


if TYPE_CHECKING:
    from primer.agent.tool_manager import ToolExecutionManager
    from primer.int.llm import LLM
    from primer.model.agent import Agent
    from primer.model.provider import LLMModel


logger = logging.getLogger(__name__)

# Cap the number of LLM → tool → LLM round-trips per user_message.
# Each iteration adds the tool results back to the prompt and re-asks
# the LLM. Without a cap a misbehaving model could loop forever; the
# turn aborts with an error row beyond this.
_MAX_TOOL_ROUND_TRIPS = 8


# Yielding tools that the chat surface handles as a conversational
# pause (soft_yield records a pending_tool_call; the human's next
# message resolves it). Every other yielding tool is out of scope on
# the chat surface and is failed closed inline as a tool error.
_SOFT_YIELD_TOOLS = frozenset({"ask_user", "_approval"})


def _is_soft_yield_tool(exc: YieldToWorker) -> bool:
    """True when the yield is one the chat surface can pause on."""
    return exc.yielded.tool_name in _SOFT_YIELD_TOOLS


def _is_switch_tool(exc: YieldToWorker) -> bool:
    """True when the yield is a switch_to_agent handoff."""
    return exc.yielded.tool_name == "switch_to_agent"


# Substrings that indicate the upstream model/provider refused our
# request because of a multimodal content part (image / document /
# audio / video) rather than a generic protocol failure. LM Studio +
# vLLM + many OpenAI-compatible local servers report this as a 400
# with ``invalid_union`` on the ``input`` parameter; cloud providers
# tend to use ``unsupported_content`` / ``unsupported_value``. Match
# loosely — a friendly diagnosis is better than leaking raw provider
# error strings to operators.
_ATTACHMENT_REJECTION_MARKERS = (
    "invalid_union",
    "invalid type for 'input'",
    "invalid type for input",
    "unsupported_content",
    "unsupported content type",
    "image_url is not supported",
    "input_file is not supported",
    "input_image is not supported",
)


# Chat list titles are rendered in a single table column; longer
# than this looks broken in the UI. The hard cap matches
# :attr:`Chat.title`'s ``max_length`` so the truncated title can't
# overflow Pydantic validation on the next ``chat.update()`` call.
_TITLE_MAX_CHARS = 80


def _derive_chat_title(parts: list) -> str:
    """Pick the first non-empty TextPart's text and trim it to a
    chat-list-friendly length, collapsing whitespace runs.

    Falls back to a generic ``"[attachment]"`` placeholder when the
    turn carries only binary parts (image/document/audio/video) —
    the operator can rename later (TODO: title-edit affordance) but
    the list view stays readable in the meantime.
    """
    for part in parts:
        text = getattr(part, "text", None)
        if not isinstance(text, str):
            continue
        cleaned = " ".join(text.split())
        if not cleaned:
            continue
        if len(cleaned) <= _TITLE_MAX_CHARS:
            return cleaned
        # Trim on a word boundary if one exists in the back third, so
        # the title doesn't snap a word in half when it can be avoided.
        truncated = cleaned[: _TITLE_MAX_CHARS - 1]
        space = truncated.rfind(" ")
        if space >= _TITLE_MAX_CHARS * 2 // 3:
            truncated = truncated[:space]
        return truncated + "…"
    return "[attachment]"


def _text_of(reply_msg: "ChatMessage") -> str:
    """Extract the plain text of a persisted ``user_message`` row.

    Mirrors :func:`primer.chat.dispatch._parts_from` / the user_message
    branch of :meth:`_load_history`: prefer the structured ``parts``
    payload (join every TextPart's text), fall back to the flattened
    ``content`` field for legacy / text-only rows.
    """
    payload = reply_msg.payload or {}
    raw_parts = payload.get("parts")
    if isinstance(raw_parts, list) and raw_parts:
        texts: list[str] = []
        for entry in raw_parts:
            if isinstance(entry, dict):
                text = entry.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
        if texts:
            return "\n".join(texts)
    content = payload.get("content")
    return content if isinstance(content, str) else ""


def _looks_like_attachment_rejection(exc: Exception) -> bool:
    """Match the exception text against well-known multimodal rejection
    markers. Used both to gate the friendly diagnosis and to decide
    whether to sanitize the persisted history (callers can re-use the
    same signal so the two stay in sync)."""
    haystack = str(exc).lower()
    return any(m in haystack for m in _ATTACHMENT_REJECTION_MARKERS)


def _diagnose_unsupported_attachment(
    *,
    exc: Exception,
    prompt_messages: list,
    model_name: str,
) -> str | None:
    """If the failure looks like 'model rejected our attachment', return
    a friendlier explanation tailored to the rejected modality kind(s);
    otherwise return None so the caller falls back to the raw exception
    message.

    Scans every part across the prompt (system + history + new user
    message), not just the current turn's parts. When the operator
    sent a PDF on turn 1 and got rejected, the second turn that looks
    text-only ALSO fails because history still carries the rejected
    part — the diagnosis (and the sanitization that follows it) needs
    to fire in both cases.
    """
    if not _looks_like_attachment_rejection(exc):
        return None
    nontext_kinds: set[str] = set()
    for msg in prompt_messages:
        for p in getattr(msg, "parts", []) or []:
            if type(p).__name__ == "TextPart":
                continue
            nontext_kinds.add(
                getattr(p, "type", type(p).__name__.lower()),
            )
    if not nontext_kinds:
        return None
    kinds_label = ", ".join(sorted(nontext_kinds))

    # Tailor the remediation hint to what actually got rejected. A
    # vision model (Qwen-VL, Llama-Vision) accepts images but not
    # PDFs — telling its operator to "use a multimodal model" is
    # technically wrong since their model IS multimodal for images.
    if nontext_kinds == {"document"}:
        remediation = (
            "PDFs and other documents require specific model support — "
            "try gpt-4o family, gpt-4.1, Claude 3.5+, or Gemini 1.5+. "
            "Most local + vision-only models accept images but not "
            "documents. You can also extract the document's text and "
            "paste it inline."
        )
    elif nontext_kinds == {"image"}:
        remediation = (
            "This model doesn't accept image input. Switch to a "
            "vision-capable model (Qwen-VL, gpt-4o family, Claude 3.5+, "
            "Gemini, Llama-Vision) or describe the image in text."
        )
    else:
        remediation = (
            "This model doesn't accept all the attached content types. "
            "Try a more capable multimodal model (gpt-4o family, "
            "Claude 3.5+, Gemini 1.5+) or remove the attachments."
        )

    return (
        f"The model {model_name!r} rejected the {kinds_label} attached "
        f"to this conversation. {remediation} "
        "The attachment has been removed from the chat history so you "
        "can continue this conversation without resending it."
    )


class ChatTurnRunner:
    """Drive one user_message → assistant_reply round-trip against an LLM."""

    def __init__(
        self,
        *,
        agent: "Agent",
        llm: "LLM",
        llm_model: "LLMModel",
        tool_manager: "ToolExecutionManager",
        chat_storage: Storage[Chat],
        message_storage: Storage[ChatMessage],
        cancel_event: asyncio.Event | None = None,
        artifact_storage: object | None = None,
        approval_record_storage: object | None = None,
    ) -> None:
        self._agent = agent
        self._llm = llm
        self._model = llm_model
        self._tools = tool_manager
        self._chats = chat_storage
        self._messages = message_storage
        self._cancel_event = cancel_event
        # Optional storage for durable resolved tool-approval records. When
        # wired, an approval resolved on the chat surface (operator yes/no, or
        # a cancel-while-awaiting) writes a ToolApprovalRecord. None -> skip.
        self._approval_records = approval_record_storage
        # Optional artifact store: when a tool returns media (MCP image/audio),
        # convert + store it so the tool_result row carries media parts the
        # channel relay can forward. None -> tool media is not surfaced.
        self._artifacts = artifact_storage
        # Pre-turn auto-compaction state.
        self._marker_persisted: bool = False
        # Last Usage event consumed from the LLM stream; populated by
        # ``_record_usage`` when the provider reports per-turn counts.
        self._last_input_tokens: int | None = None
        self._last_output_tokens: int | None = None

    async def _tool_media_parts(self, rp: "ToolResultPart") -> list:
        """Convert a tool result's media blocks into artifact-backed media
        parts (stored bytes + artifact_id). [] when no artifact store is wired
        or the result carried no media."""
        media = getattr(rp, "media", None)
        if not media or self._artifacts is None:
            return []
        from primer.channel.media import parts_from_tool_media
        try:
            return await parts_from_tool_media(self._artifacts, media)
        except Exception:
            logger.warning("tool media capture failed; skipping", exc_info=True)
            return []

    async def run_turn(
        self,
        chat: Chat,
        user_input: "str | list",
        *,
        already_persisted_user_msg: "ChatMessage | None" = None,
    ) -> AsyncIterator[ChatMessage]:
        """Persist + stream rows for one chat turn.

        ``user_input`` accepts either a plain string (legacy text-only
        callers) or a pre-validated list of :class:`Part` objects (the
        WS handler hands these in directly so multimodal attachments
        survive the round-trip).

        ``already_persisted_user_msg``: when the caller (e.g. the
        worker-side dispatch) has already persisted the user_message row
        to storage, pass it here. The runner will skip the persistence
        step, yield the provided row as-is, and use it as the anchor for
        ``last_seq`` so subsequent rows are appended after it. The
        ``chat.last_seq`` field is NOT updated to match (the caller
        should ensure it is already ≥ the row's seq before calling).
        """
        # Stash the active chat id so ``_record_usage`` can route the
        # Usage event into the per-chat cache (spec §6.4).
        self._active_chat_id = chat.id

        # Normalise to a list of Parts.
        if isinstance(user_input, str):
            parts: list = [TextPart(text=user_input)]
        else:
            parts = list(user_input)

        # 1) Persist user_message + yield so the UI echoes it back —
        # OR re-use an already-persisted row passed by the caller.
        if already_persisted_user_msg is not None:
            user_msg = already_persisted_user_msg
            # Align chat.last_seq so subsequent _append calls place rows
            # immediately after this message.
            if chat.last_seq < user_msg.seq:
                chat.last_seq = user_msg.seq
            # Stamp title from the pre-persisted message's parts/content
            # if the chat doesn't have one yet. Mirrors the normal path.
            if chat.title is None:
                chat.title = _derive_chat_title(parts)
            yield user_msg
        else:
            # The payload mirrors both shapes so cursor-replay clients see
            # the structured parts AND the flattened text (the latter is
            # the field the existing UI bubble extractor reads).
            flat_text = "\n".join(
                p.text for p in parts if isinstance(p, TextPart) and p.text
            )
            payload: dict[str, Any] = {
                "parts": [p.model_dump(mode="json") for p in parts],
            }
            if flat_text:
                payload["content"] = flat_text

            # Stamp the chat title from the first user_message text the
            # FIRST time we see one — preserves the originating intent for
            # the chats-list view even as the conversation evolves. Never
            # overwrite once set. ``_append`` below already persists the
            # chat row (it bumps last_seq), so this rides on the same
            # storage round-trip — no extra write.
            if chat.title is None:
                chat.title = _derive_chat_title(parts)

            user_msg = await self._append(
                chat,
                kind="user_message",
                payload=payload,
            )
            yield user_msg

        # 2) Build the prompt: system + coalesced history + new user msg.
        # When the dispatch flow pre-persists the user_message AND has
        # queued additional user_messages behind it, the rows table holds
        # future-turn input we must NOT leak into this turn's history.
        history = await self._load_history(
            chat.id, current_user_msg_seq=user_msg.seq,
        )
        # Pre-turn auto-compaction: if the prompt would exceed budget,
        # summarise the head + persist a marker row. ``history`` is
        # mutated in place so the prompt below carries the compacted
        # form. Failures fall through with the un-compacted history —
        # better to risk an oversize prompt than to abort the turn.
        try:
            compacted = await self._maybe_compact_history(chat, history)
        except Exception:  # noqa: BLE001
            logger.exception(
                "ChatTurnRunner: pre-turn compaction failed for chat %s; "
                "continuing with un-compacted history",
                chat.id,
            )
            compacted = False
        if compacted:
            logger.info(
                "ChatTurnRunner: pre-turn compaction fired for chat %s",
                chat.id,
            )
        new_user_msg = Message(role="user", parts=parts)
        prompt = self._build_prompt(history, new_user_msg)

        async for row in self._run_llm_loop(chat, prompt):
            yield row

    async def continue_turn(
        self, chat: Chat,
    ) -> AsyncIterator[ChatMessage]:
        """Re-enter the agent loop from persisted history alone.

        Used by the resume path after a pending tool_call has been
        resolved (its tool_result persisted + the consumed reply
        ``_history_excluded``). Builds the prompt purely from history:
        no new user_message is injected or echoed, then runs the same
        LLM → tool → LLM loop as :meth:`run_turn`. The augmented history
        now pairs the previously-unresolved ``tool_call`` with its
        ``tool_result`` so the model resumes mid-conversation.
        """
        self._active_chat_id = chat.id
        # ``current_user_msg_seq=None``: the resolved tool_result is the
        # trailing row now, and the consumed reply user_message carries
        # ``_history_excluded`` so _load_history drops it. The None path
        # only trims a trailing *user_message*, which no longer exists.
        history = await self._load_history(chat.id)
        try:
            compacted = await self._maybe_compact_history(chat, history)
        except Exception:  # noqa: BLE001
            logger.exception(
                "ChatTurnRunner: pre-resume compaction failed for chat %s; "
                "continuing with un-compacted history",
                chat.id,
            )
            compacted = False
        if compacted:
            logger.info(
                "ChatTurnRunner: pre-resume compaction fired for chat %s",
                chat.id,
            )
        prompt = self._build_prompt(history, None)
        async for row in self._run_llm_loop(chat, prompt):
            yield row

    async def _run_llm_loop(
        self, chat: Chat, prompt: list[Message],
    ) -> AsyncIterator[ChatMessage]:
        """Run the LLM → tool → LLM round-trip loop over ``prompt``.

        Shared by :meth:`run_turn` (fresh user turn) and
        :meth:`continue_turn` (resume from history). Persists + yields
        every row; terminates on a done/error/cancelled row or the
        round-trip cap.
        """
        # Resolve the tool catalogue once. Empty when the agent has
        # no toolsets configured.
        try:
            tools = await self._tools.list_tools()
        except Exception as exc:  # noqa: BLE001 — surface as error row.
            logger.exception(
                "ChatTurnRunner: list_tools failed for agent %s: %s",
                self._agent.id, exc,
            )
            err = await self._append(
                chat,
                kind="error",
                payload={"message": f"tool listing failed: {exc}"},
            )
            yield err
            return

        # LLM → tool → LLM loop.
        # The loop's hard bound must NOT shadow a higher per-agent cap:
        # when ``max_tool_turns`` is set it is the authority (allow up to
        # that many rounds); when it is None we fall back to
        # ``_MAX_TOOL_ROUND_TRIPS`` so an unbounded opt-out still can't
        # spin forever. The per-agent terminal error below still fires at
        # the configured cap.
        max_turns = self._agent.max_tool_turns
        loop_bound = (
            max(_MAX_TOOL_ROUND_TRIPS, max_turns)
            if max_turns is not None
            else _MAX_TOOL_ROUND_TRIPS
        )
        tool_round = 0
        for _ in range(loop_bound):
            tool_calls: list[ToolCallPart] = []
            tool_names: dict[str, str] = {}  # ToolCallStart.id → name
            assistant_text_parts: list[str] = []
            terminal_done: Done | None = None
            saw_error = False

            try:
                async for event in self._llm.stream(
                    model=self._model.name,
                    messages=prompt,
                    tools=tools or None,
                ):
                    async for row in self._handle_event(
                        event=event,
                        chat=chat,
                        tool_calls=tool_calls,
                        tool_names=tool_names,
                        assistant_text_parts=assistant_text_parts,
                    ):
                        yield row
                    if self._cancel_event is not None and self._cancel_event.is_set():
                        cancelled = await self._append(
                            chat,
                            kind="cancelled",
                            payload={"reason": "operator_interrupt"},
                        )
                        yield cancelled
                        return
                    if isinstance(event, Done):
                        terminal_done = event
                        break
                    if isinstance(event, Error):
                        saw_error = True
                        break
            except Exception as exc:  # noqa: BLE001 — surface as error row.
                logger.exception(
                    "ChatTurnRunner: LLM stream failed for chat %s: %s",
                    chat.id, exc,
                )
                friendly = _diagnose_unsupported_attachment(
                    exc=exc,
                    prompt_messages=prompt,
                    model_name=self._model.name,
                )
                if friendly is not None:
                    # Strip the rejected binary parts from every
                    # persisted user_message row so the next turn
                    # doesn't replay the same failure (the chat would
                    # otherwise be permanently broken — every fresh
                    # message re-loads the broken history and 400s
                    # again with no clue why).
                    try:
                        await self._sanitize_unsupported_attachments(chat)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "ChatTurnRunner: failed to sanitize history "
                            "after attachment rejection for chat %s",
                            chat.id,
                        )
                err = await self._append(
                    chat,
                    kind="error",
                    payload={"message": friendly or f"llm stream failed: {exc}"},
                )
                yield err
                return

            if saw_error:
                return

            # Append the assistant turn to the prompt so subsequent
            # iterations (tool round-trip) carry the model's prior
            # output, mirroring the agent loop in primer/agent/loop.py.
            assistant_parts: list[Any] = []
            if assistant_text_parts:
                assistant_parts.append(
                    TextPart(text="".join(assistant_text_parts)),
                )
            for tc in tool_calls:
                assistant_parts.append(tc)
            if assistant_parts:
                prompt.append(
                    Message(role="assistant", parts=assistant_parts),
                )

            stop_reason = terminal_done.stop_reason if terminal_done else "stop"

            if stop_reason != "tool_use" or not tool_calls:
                done_msg = await self._append(
                    chat,
                    kind="done",
                    payload={"stop_reason": stop_reason},
                )
                yield done_msg
                return

            # Per-agent tool-turn cap. The model wants another tool round;
            # enforce ``agent.max_tool_turns`` BEFORE dispatching it so a
            # model that never stops cannot loop unbounded.
            tool_round += 1
            if max_turns is not None and tool_round >= max_turns:
                cap_err = await self._append(
                    chat,
                    kind="error",
                    payload={
                        "message": (
                            f"turn stopped: reached max tool turns "
                            f"({max_turns})"
                        ),
                        "code": "max_tool_turns_exceeded",
                    },
                )
                yield cap_err
                return

            # Dispatch each tool call; persist the results back as
            # tool_result rows + append to the prompt for the next pass.
            #
            # A yielding tool (ask_user / approval gate) raises
            # YieldToWorker. Only ONE pending_tool_call slot exists per
            # chat, so the FIRST yield in the batch becomes pending and
            # the soft-yield path fills its tool_result on resume. Every
            # OTHER tool_call in the SAME assistant batch (calls AFTER
            # the yielding one that never ran, plus any SECOND yielding
            # call) must still get a synthetic tool_result here, or the
            # persisted history keeps an unpaired tool_use and the
            # provider 400s on the continuation.
            tool_result_parts: list[ToolResultPart] = []
            yielded_idx: int | None = None
            yield_exc: YieldToWorker | None = None
            for idx, tc in enumerate(tool_calls):
                try:
                    rp = await self._tools.execute(tc)
                except YieldToWorker as exc:
                    if not _is_soft_yield_tool(exc) and not _is_switch_tool(exc):
                        # Out of scope on the chat surface (mcp_task
                        # deferred; sleep/watch unreachable). Fail closed
                        # inline like a normal tool error so the agent
                        # sees the result and the loop continues to a
                        # terminal, NOT a pending pause (no soft_yield).
                        rp = ToolResultPart(
                            id=tc.id,
                            output=(
                                f"{exc.yielded.tool_name!r} is not supported "
                                "on the chat surface"
                            ),
                            error=True,
                        )
                    else:
                        yielded_idx = idx
                        yield_exc = exc
                        break
                except Exception as exc:  # noqa: BLE001 — model-visible error
                    rp = ToolResultPart(
                        id=tc.id,
                        output=f"tool {tc.name!r} raised: {exc}",
                        error=True,
                    )
                tool_result_parts.append(rp)
                payload = {
                    "id": tc.id,
                    "name": tc.name,
                    "result": rp.output,
                    "error": bool(rp.error),
                }
                media_parts = await self._tool_media_parts(rp)
                if media_parts:
                    payload["media"] = [
                        p.model_dump(mode="json") for p in media_parts
                    ]
                yield await self._append(
                    chat,
                    kind="tool_result",
                    payload=payload,
                )

            if yield_exc is not None:
                # Pair every un-run/secondary tool_call in this batch with
                # a synthetic error tool_result so the history stays valid,
                # then re-raise so the dispatch layer's soft_yield records
                # the FIRST yielding call as the single pending_tool_call.
                yielding_tc = tool_calls[yielded_idx]
                for other in tool_calls[yielded_idx + 1:]:
                    yield await self._append(
                        chat,
                        kind="tool_result",
                        payload={
                            "id": other.id,
                            "name": other.name,
                            "result": (
                                f"skipped: turn paused on "
                                f"{yielding_tc.name!r}"
                            ),
                            "error": True,
                        },
                    )
                raise yield_exc

            prompt.append(
                Message(role="tool", parts=list(tool_result_parts)),
            )
            # Loop — re-ask the LLM with the tool results appended.

        # Round-trip limit hit. Emit an error row.
        cap_err = await self._append(
            chat,
            kind="error",
            payload={
                "message": (
                    f"tool round-trip limit reached "
                    f"({_MAX_TOOL_ROUND_TRIPS}); aborting turn"
                ),
            },
        )
        yield cap_err

    async def soft_yield(self, chat: Chat, exc: YieldToWorker) -> None:
        """Convert a chat-surface yield into a conversational pause: surface the
        tool's prompt as a visible assistant message, record the pending tool
        call on the chat row, and let the turn end. The human's next message
        resolves it (resume path). No parking."""
        y = exc.yielded
        tool_call_id = exc.tool_call_id
        meta = y.resume_metadata or {}
        if y.tool_name == "_approval":
            original = meta.get("original_call") or {}
            reason = meta.get("gate_reason") or ""
            prompt = (
                f"I'd like to run `{original.get('name', '?')}`"
                + (f" ({reason})" if reason else "")
                + ". Approve? (yes/no)"
            )
            pending = {"tool_call_id": tool_call_id, "mode": "approval",
                       "original_call": original,
                       "policy_id": meta.get("policy_id"),
                       "approval_type": meta.get("approval_type"),
                       "gate_reason": meta.get("gate_reason")}
        elif y.tool_name == "ask_user":
            prompt = meta.get("prompt") or ""
            pending = {"tool_call_id": tool_call_id, "mode": "ask_user",
                       "response_schema": meta.get("response_schema")}
        else:
            # Out of scope on the chat surface (mcp_task deferred; sleep/watch
            # unreachable). Fail closed so the agent is not stuck.
            await self._append(chat, kind="tool_result", payload={
                "id": tool_call_id, "name": y.tool_name,
                "result": f"{y.tool_name!r} is not supported on the chat surface",
                "error": True,
            })
            return
        await self._append(chat, kind="assistant_token", payload={"delta": prompt})
        chat.pending_tool_call = pending
        await self._persist_chat(chat)

    async def handle_switch(self, chat: Chat, exc: YieldToWorker) -> None:
        """End the turn and stage a handoff to another agent. The dispatch loop
        injects ``pending_handoff`` as the next turn (run by the new agent)."""
        meta = exc.yielded.resume_metadata or {}
        chat.agent_id = meta.get("agent_id") or chat.agent_id
        chat.pending_handoff = meta.get("prompt") or ""
        # Authoritative write of THIS switch (NOT via _persist_chat, which would
        # re-read the old agent_id from storage and clobber our own change).
        await self._chats.update(chat)

    async def _write_chat_approval_record(
        self, *, chat: Chat, pending: dict, decision: str, reason: str | None,
    ) -> None:
        """Persist a resolved approval decision for a chat gate (best-effort)."""
        from primer.agent.approval_record import (
            record_from_chat_pending,
            write_approval_record,
        )
        record = record_from_chat_pending(
            pending=pending,
            decision=decision,
            reason=reason,
            chat_id=chat.id,
            agent_id=getattr(chat, "agent_id", None),
            requested_at=getattr(chat, "created_at", None),
        )
        await write_approval_record(self._approval_records, record)

    async def abandon_pending(self, chat: Chat, pending: dict) -> None:
        """Abandon a pending (awaiting-input) tool call on cancel. Delegates to
        the shared helper so the switch endpoint can reuse the same logic. The
        helper records the cancellation when the gate is an approval."""
        from primer.chat.pending import abandon_pending_rows
        await abandon_pending_rows(
            chat, pending=pending, messages=self._messages, chats=self._chats,
            result_text="cancelled by user",
            terminal_reason="cancel_while_awaiting_input",
            approval_records=self._approval_records,
        )

    # Tokens that read as an affirmative approval. Matched case-folded
    # against the reply's whitespace-split tokens.
    _AFFIRMATIVE = {"yes", "y", "approve", "approved", "ok", "okay", "sure", "go"}
    # Tokens that read as a refusal. A negative anywhere in the reply
    # vetoes a co-occurring affirmative ("no yes" -> rejected) so the
    # parse fails closed against ambiguous intent.
    _NEGATIVE = {
        "no", "n", "nope", "nah", "deny", "denied", "reject", "rejected",
        "cancel", "stop", "dont", "don't", "do not",
    }

    async def resume_pending(
        self, chat: Chat, pending: dict, reply_msg: ChatMessage,
    ) -> None:
        """Consume the human's reply as the pending tool call's result.

        For ``ask_user`` the reply text becomes the tool_result output.
        For ``approval`` an affirmative reply re-runs the original call
        (gate bypassed); a negative reply records a rejection result and
        does NOT execute. Persists the tool_result row (pairing the
        unresolved tool_call), flags the consumed reply
        ``_history_excluded`` so it is not replayed as a fresh user turn,
        and clears ``chat.pending_tool_call``.
        """
        tool_call_id = pending["tool_call_id"]
        reply_text = _text_of(reply_msg)
        mode = pending.get("mode")
        if mode == "approval":
            decision = reply_text.strip().lower()
            tokens = decision.split()
            has_neg = any(t in self._NEGATIVE for t in tokens)
            has_aff = any(t in self._AFFIRMATIVE for t in tokens)
            approved = has_aff and not has_neg
            # Persist the resolved decision (exactly once) BEFORE acting on it,
            # so the operator's verdict is durable even if the re-dispatch
            # itself later errors. reply_text is the operator's free-text reason.
            await self._write_chat_approval_record(
                chat=chat,
                pending=pending,
                decision="approved" if approved else "rejected",
                reason=None if approved else reply_text,
            )
            if approved:
                original = pending.get("original_call") or {}
                call = ToolCallPart(
                    id=original.get("id") or tool_call_id,
                    name=original.get("name") or "",
                    arguments=original.get("arguments") or {},
                )
                rp = await self._tools.execute(call, bypass_approval=True)
                result_out, is_err = rp.output, bool(rp.error)
            else:
                result_out, is_err = (
                    "user declined to approve the tool call", True,
                )
        else:  # ask_user
            result_out, is_err = reply_text, False
        await self._append(chat, kind="tool_result", payload={
            "id": tool_call_id, "name": str(mode or ""),
            "result": result_out, "error": is_err,
        })
        excluded = reply_msg.model_copy(update={
            "payload": {**(reply_msg.payload or {}), "_history_excluded": True},
        })
        await self._messages.update(excluded)
        chat.pending_tool_call = None
        await self._persist_chat(chat)

    # ------------------------------------------------------------------
    # Event translation
    # ------------------------------------------------------------------

    async def _handle_event(
        self,
        *,
        event: StreamEvent,
        chat: Chat,
        tool_calls: list[ToolCallPart],
        tool_names: dict[str, str],
        assistant_text_parts: list[str],
    ) -> AsyncIterator[ChatMessage]:
        """Translate one :class:`StreamEvent` into ChatMessage rows."""
        if isinstance(event, TextDelta):
            assistant_text_parts.append(event.text)
            yield await self._append(
                chat,
                kind="assistant_token",
                payload={"delta": event.text},
            )
            return

        if isinstance(event, ToolCallStart):
            tool_names[event.id] = event.name
            return

        if isinstance(event, ToolCallEnd):
            name = tool_names.get(event.id, "<unknown>")
            tc = ToolCallPart(
                id=event.id,
                name=name,
                arguments=event.arguments,
            )
            tool_calls.append(tc)
            yield await self._append(
                chat,
                kind="tool_call",
                payload={
                    "id": event.id,
                    "name": name,
                    "args": event.arguments,
                },
            )
            return

        if isinstance(event, Error):
            yield await self._append(
                chat,
                kind="error",
                payload={
                    "message": event.message or "llm error",
                    "code": getattr(event, "code", None),
                },
            )
            return

        if isinstance(event, Usage):
            self._record_usage(event)
            return

        # StreamStart / ReasoningDelta / Done / MediaDelta /
        # ToolCallDelta / ExtendedEvent — silently ignored. ToolCallDelta
        # carries argument JSON fragments; ToolCallEnd already exposes
        # the parsed argument object so we don't need to buffer deltas.

    # ------------------------------------------------------------------
    # History loading + prompt building
    # ------------------------------------------------------------------

    async def _load_history(
        self,
        chat_id: str,
        *,
        current_user_msg_seq: int | None = None,
    ) -> list[Message]:
        """Read every prior :class:`ChatMessage` for the chat (in seq order).

        Coalesces consecutive ``assistant_token`` rows into a single
        assistant :class:`Message`; binds ``tool_call`` rows to the
        most recent assistant message; lifts ``tool_result`` rows into
        a ``tool``-role message. Other kinds (done/yielded/resumed/
        error) are skipped — they're protocol markers, not history.

        The current turn's user_message is excluded from history because
        the caller passes it back as a separate ``new_messages`` argument
        to :meth:`_build_prompt`. When ``current_user_msg_seq`` is given
        every row at or after that seq is dropped — this is how the
        FIFO-queued worker flow protects this turn from queued future-turn
        user_messages that are already persisted but not yet processed.
        """
        rows = await self._read_messages_full(chat_id)
        if current_user_msg_seq is not None:
            rows = [r for r in rows if r.seq < current_user_msg_seq]
        elif rows and rows[-1].kind == "user_message":
            # Inline-runner legacy path: drop the just-persisted trailing
            # user_message that the caller will re-emit via _build_prompt.
            rows = rows[:-1]

        # Rows that previously triggered a model-side rejection (e.g. a
        # tool_call/tool_result the active model can't render) are
        # flagged ``_history_excluded`` by _sanitize_unsupported_attachments
        # so subsequent turns don't replay the same upstream 400.
        rows = [r for r in rows if not (r.payload or {}).get("_history_excluded")]

        # Compaction marker reassembly: if the chat has been compacted
        # at any point, replace every row at or before the *last*
        # marker with a single synthetic assistant message carrying
        # the marker's summary text. Everything after the marker is
        # translated normally below. This keeps the live prompt small
        # without losing the rolled-up context.
        synthetic_summary: Message | None = None
        last_marker_idx = -1
        for idx, row in enumerate(rows):
            if row.kind == "compaction_marker":
                last_marker_idx = idx
        if last_marker_idx >= 0:
            marker = rows[last_marker_idx]
            summary_text = (marker.payload or {}).get("summary") or ""
            if summary_text:
                synthetic_summary = Message(
                    role="assistant",
                    parts=[TextPart(text=summary_text)],
                )
            rows = rows[last_marker_idx + 1:]

        out: list[Message] = []
        if synthetic_summary is not None:
            out.append(synthetic_summary)
        current_assistant_text: list[str] = []
        current_assistant_tools: list[ToolCallPart] = []
        current_tool_results: list[ToolResultPart] = []

        def flush_assistant() -> None:
            if not current_assistant_text and not current_assistant_tools:
                return
            parts: list[Any] = []
            if current_assistant_text:
                parts.append(TextPart(text="".join(current_assistant_text)))
            for tc in current_assistant_tools:
                parts.append(tc)
            out.append(Message(role="assistant", parts=parts))
            current_assistant_text.clear()
            current_assistant_tools.clear()

        def flush_tool_results() -> None:
            if not current_tool_results:
                return
            out.append(
                Message(role="tool", parts=list(current_tool_results)),
            )
            current_tool_results.clear()

        for row in rows:
            kind = row.kind
            payload = row.payload or {}

            if kind == "user_message":
                flush_assistant()
                flush_tool_results()
                # Prefer structured parts (new shape); fall back to
                # bare ``content`` for rows persisted before the
                # multimodal switchover.
                raw_parts = payload.get("parts")
                rebuilt: list[Any] = []
                if isinstance(raw_parts, list) and raw_parts:
                    from pydantic import TypeAdapter
                    from primer.model.chat import Part

                    adapter = TypeAdapter(Part)
                    for entry in raw_parts:
                        try:
                            rebuilt.append(adapter.validate_python(entry))
                        except Exception:  # noqa: BLE001 — skip malformed
                            continue
                if not rebuilt:
                    text = payload.get("content") or ""
                    if text:
                        rebuilt.append(TextPart(text=text))
                if rebuilt:
                    out.append(Message(role="user", parts=rebuilt))
                continue

            if kind == "assistant_token":
                flush_tool_results()
                delta = payload.get("delta") or payload.get("text") or ""
                if delta:
                    current_assistant_text.append(delta)
                continue

            if kind == "tool_call":
                flush_tool_results()
                tc_id = payload.get("id") or ""
                tc_name = payload.get("name") or ""
                tc_args = payload.get("args") or {}
                if tc_id and tc_name:
                    current_assistant_tools.append(
                        ToolCallPart(id=tc_id, name=tc_name, arguments=tc_args),
                    )
                continue

            if kind == "tool_result":
                flush_assistant()
                rp_id = payload.get("id") or ""
                rp_out = payload.get("result")
                if rp_id is not None:
                    current_tool_results.append(
                        ToolResultPart(
                            id=rp_id,
                            output=rp_out if rp_out is not None else "",
                            error=bool(payload.get("error")),
                        ),
                    )
                continue
            # done / yielded / resumed / error — boundary markers; skip.

        flush_assistant()
        flush_tool_results()
        return out

    async def _read_messages_full(
        self, chat_id: str,
    ) -> list[ChatMessage]:
        out: list[ChatMessage] = []
        cursor: str | None = None
        while True:
            page = await self._messages.find(
                Predicate(
                    left=FieldRef(name="chat_id"),
                    op=Op.EQ,
                    right=Value(value=chat_id),
                ),
                CursorPage(cursor=cursor, length=200),
                order_by=[OrderBy(field="seq", direction="asc")],
            )
            out.extend(page.items)
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                break
        return out

    # ------------------------------------------------------------------
    # Pre-turn auto-compaction
    # ------------------------------------------------------------------

    async def _maybe_compact_history(
        self,
        chat: Chat,
        history: list[Message],
    ) -> bool:
        """Run pre-turn compaction if the prompt would exceed budget.

        Returns ``True`` if compaction fired and ``history`` was
        replaced + a ``compaction_marker`` row persisted; ``False``
        otherwise. The caller passes ``history`` by reference so the
        mutated list (cleared and refilled with the compacted form) is
        observable in the turn driver.
        """
        tools = None
        try:
            tools = await self._tools.list_tools()
        except Exception:  # noqa: BLE001 — fall through with no tools.
            tools = None
        triggered, _count = await _mixin_should_compact(
            llm=self._llm,
            model_name=self._model.name,
            context_length=self._model.context_length,
            history=history,
            tools=tools or None,
        )
        if not triggered:
            return False

        strategy = CompactionStrategy()
        compaction_prompt_field = getattr(self._agent, "compaction_prompt", None)
        if compaction_prompt_field:
            compaction_prompt = "\n\n".join(compaction_prompt_field)
            prompt_source = "custom"
        else:
            compaction_prompt = DEFAULT_COMPACTION_PROMPT
            prompt_source = "default"
        result = await _mixin_apply_compaction(
            llm=self._llm,
            strategy=strategy,
            history=history,
            compaction_prompt=compaction_prompt,
            model_name=self._model.name,
            context_length=self._model.context_length,
        )
        next_seq = await self._next_seq_for_marker(chat)
        chat_msg = ChatMessage(
            id=ChatMessage.make_id(chat.id, next_seq),
            chat_id=chat.id,
            seq=next_seq,
            kind="compaction_marker",
            payload={
                "summary": result.summary_text,
                "replaced_from_seq": 1,
                "replaced_to_seq": next_seq - 1,
                "model": self._model.name,
                "tokens_before": result.tokens_before,
                "tokens_after": result.tokens_after,
                "compaction_prompt_source": prompt_source,
                "created_at": result.created_at.isoformat(),
            },
            created_at=result.created_at,
        )
        await self._messages.create(chat_msg)
        chat.last_seq = next_seq
        self._marker_persisted = True
        history[:] = result.new_history
        return True

    async def _next_seq_for_marker(self, chat: Chat) -> int:
        """Compute the next free seq for a marker row.

        Re-reads the chat row from storage to pick up any concurrent
        last_seq bumps (e.g. when the user_message has just been
        appended on a different code path). Falls back to the
        in-memory ``chat.last_seq`` if the row has gone missing.
        """
        fresh = await self._chats.get(chat.id)
        last = getattr(fresh, "last_seq", chat.last_seq) if fresh else chat.last_seq
        return last + 1

    # ------------------------------------------------------------------
    # Usage tracking
    # ------------------------------------------------------------------

    def _record_usage(self, ev: Usage) -> None:
        """Stash per-chat last input/output tokens from a ``Usage`` event.

        Also mirrors the values into :mod:`primer.chat.usage_cache` so
        the WS layer can build a ``usage`` envelope without re-running
        the prompt-build math (spec §6.4). ``_active_chat_id`` is set
        by :meth:`run_turn` before the LLM stream begins.
        """
        self._last_input_tokens = ev.input_tokens
        self._last_output_tokens = ev.output_tokens
        chat_id = getattr(self, "_active_chat_id", "") or ""
        if chat_id:
            from primer.chat.usage_cache import set_usage
            set_usage(chat_id, ev.input_tokens, ev.output_tokens)

    def _build_prompt(
        self,
        history: list[Message],
        new_user_msg: Message | None,
    ) -> list[Message]:
        prompt: list[Message] = []
        if self._agent.system_prompt:
            sys_text = "\n\n".join(self._agent.system_prompt)
            prompt.append(
                Message(role="system", parts=[TextPart(text=sys_text)]),
            )
        prompt.extend(history)
        # ``new_user_msg`` is None on the resume path (continue_turn):
        # the prompt is built purely from the augmented history.
        if new_user_msg is not None:
            prompt.append(new_user_msg)
        return prompt

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _append(
        self,
        chat: Chat,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> ChatMessage:
        """Persist one chat_message row + bump the chat's last_seq.

        Writes the chat row via :meth:`_persist_chat`, which refreshes the
        externally-mutable fields from storage first (``cancel_requested_at``
        from an ``interrupt``, ``agent_id`` from a mid-chat agent switch) so a
        concurrent change isn't clobbered by this turn's stale in-memory copy.
        """
        next_seq = chat.last_seq + 1
        row = ChatMessage(
            id=ChatMessage.make_id(chat.id, next_seq),
            chat_id=chat.id,
            seq=next_seq,
            kind=kind,  # type: ignore[arg-type]
            payload=payload,
            created_at=datetime.now(timezone.utc),
        )
        await self._messages.create(row)
        chat.last_seq = next_seq
        await self._persist_chat(chat)
        return row

    async def _persist_chat(self, chat: Chat) -> None:
        """Update the chat row, preserving fields an external actor may have
        changed concurrently while a turn holds a stale in-memory copy:
        ``cancel_requested_at`` (an interrupt frame) and ``agent_id`` (a
        mid-chat agent switch). Without this the running turn's writes would
        clobber them.
        """
        latest = await self._chats.get(chat.id)
        if latest is not None:
            chat.cancel_requested_at = latest.cancel_requested_at
            chat.agent_id = latest.agent_id
            chat.pending_handoff = latest.pending_handoff
        await self._chats.update(chat)

    async def _sanitize_unsupported_attachments(self, chat: Chat) -> int:
        """Walk the persisted chat history and rewrite every
        ``user_message`` row that still carries a non-text part.
        Replaces the row's ``parts`` with a single TextPart whose
        text is the original message text (if any) followed by a
        marker noting the removal.

        Returns the number of rows sanitized. Called from the
        attachment-rejection error path so subsequent turns load a
        clean history and don't replay the same upstream 400.

        Per-row updates instead of a bulk operation because Storage
        doesn't expose a bulk-update primitive — the in-memory rate
        is fine for chat history sizes, and Postgres / sqlite both
        round-trip a single row update in <1ms.
        """
        rows = await self._read_messages_full(chat.id)
        sanitized = 0
        for row in rows:
            payload = row.payload or {}

            # tool_call / tool_result: the model that rejected the turn
            # likely doesn't support tool use at all (e.g. text-only
            # gemma rejecting a prior web__http_request from a model
            # swap). Flag the row as history-excluded so subsequent
            # turns rebuild a clean prompt. The row stays in storage so
            # the UI/replay can still render it as historical context.
            if row.kind in ("tool_call", "tool_result"):
                if payload.get("_history_excluded"):
                    continue
                new_payload = dict(payload)
                new_payload["_history_excluded"] = True
                row.payload = new_payload
                await self._messages.update(row)
                sanitized += 1
                continue

            if row.kind != "user_message":
                continue
            raw_parts = payload.get("parts")
            if not isinstance(raw_parts, list) or not raw_parts:
                continue
            nontext = [
                p for p in raw_parts
                if isinstance(p, dict) and p.get("type") not in (None, "text")
            ]
            if not nontext:
                continue
            removed_kinds = sorted({
                p.get("type") for p in nontext if isinstance(p, dict)
            })
            removed_label = ", ".join(k for k in removed_kinds if k)
            # Keep the user's text content (if any) and append a
            # human-readable marker so the LLM can still see roughly
            # what was on the original turn.
            original_text = payload.get("content") or ""
            marker = (
                f"[attachment removed: the previously attached "
                f"{removed_label or 'file'} was dropped from history "
                "because the configured model didn't accept it]"
            )
            new_text = (
                f"{original_text}\n\n{marker}".strip()
                if original_text else marker
            )
            new_payload = dict(payload)
            new_payload["parts"] = [{"type": "text", "text": new_text}]
            new_payload["content"] = new_text
            row.payload = new_payload
            await self._messages.update(row)
            sanitized += 1
        return sanitized


__all__ = ["ChatTurnRunner"]
