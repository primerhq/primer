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

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from matrix.int.storage import Storage
from matrix.model.chat import (
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
)
from matrix.model.chats import Chat, ChatMessage
from matrix.model.storage import (
    CursorPage,
    FieldRef,
    Op,
    OrderBy,
    Predicate,
    Value,
)


if TYPE_CHECKING:
    from matrix.agent.tool_manager import ToolExecutionManager
    from matrix.int.llm import LLM
    from matrix.model.agent import Agent
    from matrix.model.provider import LLMModel


logger = logging.getLogger(__name__)

# Cap the number of LLM → tool → LLM round-trips per user_message.
# Each iteration adds the tool results back to the prompt and re-asks
# the LLM. Without a cap a misbehaving model could loop forever; the
# turn aborts with an error row beyond this.
_MAX_TOOL_ROUND_TRIPS = 8


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
    ) -> None:
        self._agent = agent
        self._llm = llm
        self._model = llm_model
        self._tools = tool_manager
        self._chats = chat_storage
        self._messages = message_storage

    async def run_turn(
        self, chat: Chat, user_input: "str | list",
    ) -> AsyncIterator[ChatMessage]:
        """Persist + stream rows for one chat turn.

        ``user_input`` accepts either a plain string (legacy text-only
        callers) or a pre-validated list of :class:`Part` objects (the
        WS handler hands these in directly so multimodal attachments
        survive the round-trip).
        """
        # Normalise to a list of Parts.
        if isinstance(user_input, str):
            parts: list = [TextPart(text=user_input)]
        else:
            parts = list(user_input)

        # 1) Persist user_message + yield so the UI echoes it back.
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
        user_msg = await self._append(
            chat,
            kind="user_message",
            payload=payload,
        )
        yield user_msg

        # 2) Build the prompt: system + coalesced history + new user msg.
        history = await self._load_history(chat.id)
        new_user_msg = Message(role="user", parts=parts)
        prompt = self._build_prompt(history, new_user_msg)

        # 3) Resolve the tool catalogue once. Empty when the agent has
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

        # 4) LLM → tool → LLM loop.
        for _ in range(_MAX_TOOL_ROUND_TRIPS):
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
                err = await self._append(
                    chat,
                    kind="error",
                    payload={"message": f"llm stream failed: {exc}"},
                )
                yield err
                return

            if saw_error:
                return

            # Append the assistant turn to the prompt so subsequent
            # iterations (tool round-trip) carry the model's prior
            # output, mirroring the agent loop in matrix/agent/loop.py.
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

            # Dispatch each tool call; persist the results back as
            # tool_result rows + append to the prompt for the next pass.
            tool_result_parts: list[ToolResultPart] = []
            for tc in tool_calls:
                try:
                    rp = await self._tools.execute(tc)
                except Exception as exc:  # noqa: BLE001 — model-visible error
                    rp = ToolResultPart(
                        id=tc.id,
                        output=f"tool {tc.name!r} raised: {exc}",
                        error=True,
                    )
                tool_result_parts.append(rp)
                yield await self._append(
                    chat,
                    kind="tool_result",
                    payload={
                        "id": tc.id,
                        "name": tc.name,
                        "result": rp.output,
                        "error": bool(rp.error),
                    },
                )

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

        # StreamStart / ReasoningDelta / Usage / Done / MediaDelta /
        # ToolCallDelta / ExtendedEvent — silently ignored. ToolCallDelta
        # carries argument JSON fragments; ToolCallEnd already exposes
        # the parsed argument object so we don't need to buffer deltas.

    # ------------------------------------------------------------------
    # History loading + prompt building
    # ------------------------------------------------------------------

    async def _load_history(self, chat_id: str) -> list[Message]:
        """Read every prior :class:`ChatMessage` for the chat (in seq order).

        Coalesces consecutive ``assistant_token`` rows into a single
        assistant :class:`Message`; binds ``tool_call`` rows to the
        most recent assistant message; lifts ``tool_result`` rows into
        a ``tool``-role message. Other kinds (done/yielded/resumed/
        error) are skipped — they're protocol markers, not history.

        The just-persisted ``user_message`` row for the current turn
        is excluded from history because the caller passes it back as
        a separate ``new_messages`` argument to :meth:`_build_prompt`.
        """
        rows = await self._read_messages_full(chat_id)
        # Exclude the trailing user_message row (the current turn's
        # input — added back as new_messages in _build_prompt).
        if rows and rows[-1].kind == "user_message":
            rows = rows[:-1]

        out: list[Message] = []
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
                    from matrix.model.chat import Part

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

    def _build_prompt(
        self,
        history: list[Message],
        new_user_msg: Message,
    ) -> list[Message]:
        prompt: list[Message] = []
        if self._agent.system_prompt:
            sys_text = "\n\n".join(self._agent.system_prompt)
            prompt.append(
                Message(role="system", parts=[TextPart(text=sys_text)]),
            )
        prompt.extend(history)
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
        """Persist one chat_message row + bump the chat's last_seq."""
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
        await self._chats.update(chat)
        return row


__all__ = ["ChatTurnRunner"]
