"""History split and text rendering for agent-runtime replay.

Provider-neutral: a future Codex runtime reuses this module, so nothing here
imports or mentions Claude in particular.

A provider step's ``messages`` split into three parts:

* **System** -- every ``system``-role message, joined in order.
* **Tail** -- the last message, EXCEPT when the history ends in tool results:
  then the tail is the assistant message that made those calls plus the tool
  result messages after it (the one pending round the model must answer).
* **Older history** -- everything else, in order.

The older history is rendered as text appended to the system text inside a
``<conversation_history>`` block: ``[user] ...``, ``[assistant] ...``,
``[assistant -> tool call] <name> <json args>``, ``[tool result <name>] ...``
(a failed result renders ``[tool result <name> error] ...``); images and other
media render as a placeholder naming the media type. A part's text keeps its
newlines (the first line follows the label, each continuation line is indented
by two spaces) and a ``</conversation_history>`` inside the content is escaped
(``<\\/conversation_history>``) so it cannot end the block early. Reasoning
from earlier steps is dropped (it never reaches here anyway:
primer persists reasoning as ``REASONING`` event records, which the history
reader skips -- only ``Message``-shaped lines become LLM history, and the
``Part`` union carries no reasoning member; any part type this renderer does
not know is dropped rather than rendered as garbage).

The rendering only APPENDS: replaying a longer history produces a byte-identical
prefix of the system text, so prompt caching can reuse it. Two details keep it
true when provider tool ids are REUSED across rounds (they do: see
``primer/agent/approval_record.py``):

* the id-to-name map is built INCREMENTALLY while walking the older messages in
  order, so a tool result sees only the latest EARLIER call with that id -- a
  later round reusing the id can never relabel an earlier line;
* a result whose call is not in the older history falls back to the RAW id
  (never through ``name_of``: it is a call id, not a tool name).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from primer.model.chat import (
    AudioPart,
    DocumentPart,
    ExtendedPart,
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    VideoPart,
)
from primer.model.except_ import BadRequestError


@dataclass(frozen=True)
class ReplayPlan:
    """The step's system text (with rendered older history) and the structured
    tail a later task sends to the runtime on stdin."""

    system_text: str
    tail: list[Message]


def replay_tool_id(original: str) -> str:
    """A deterministic ``toolu_``-shaped tool id for the replay.

    ``toolu_``-shaped ids pass through unchanged; anything else maps to
    ``"toolu_" + sha256(original)[:24]`` so a call and its result always carry
    the same id.
    """
    if original.startswith("toolu_"):
        return original
    return "toolu_" + hashlib.sha256(original.encode("utf-8")).hexdigest()[:24]


def split_tail(messages: Sequence[Message]) -> tuple[list[Message], list[Message]]:
    """Split ``messages`` into (older history, tail) the replay way.

    The tail is the last non-system message, EXCEPT when the history ends in
    tool results: then it is the assistant message that made those calls plus
    the tool result messages after it (the one pending round the model must
    answer); system messages are in neither. Raises
    :class:`BadRequestError` when the history does not end with a user or tool
    message (empty, or ending in an assistant message): that history cannot be
    replayed.
    """
    older, tail = _partition_history(messages)
    if not tail or tail[-1].role not in ("user", "tool"):
        raise BadRequestError("history must end with a user or tool message")
    return older, tail


def _partition_history(messages: Sequence[Message]) -> tuple[list[Message], list[Message]]:
    non_system = [m for m in messages if m.role != "system"]
    if non_system and non_system[-1].role == "tool":
        # The history ends in tool results: walk back past them to the
        # assistant message that made the calls.
        i = len(non_system) - 1
        while i >= 0 and non_system[i].role == "tool":
            i -= 1
        if i >= 0 and non_system[i].role == "assistant":
            return non_system[:i], non_system[i:]
        # tool results with no making assistant in the history (malformed)
        return non_system[: i + 1], non_system[i + 1:]
    return non_system[:-1], non_system[-1:]


#: Any letter case of the history's closing tag, as it may appear in rendered
#: content (spec 6.4 v1.6).
_HISTORY_TAG = re.compile(r"</(conversation_history)>", re.IGNORECASE)


def _escape_history_tag(text: str) -> str:
    """Escape ``</conversation_history>`` in any letter case (``</`` becomes
    ``<\\/``), keeping the tag's original casing (spec 6.4 v1.6). In JSON
    arguments ``\\/`` is a valid escape of ``/``, so rendered args still decode
    to the same value."""
    return _HISTORY_TAG.sub(lambda m: "<\\/" + m.group(1) + ">", text)


def _content(text: str) -> str:
    """A part's text inside the rendered history (spec 6.4 v1.5).

    Newlines are kept: the first line follows the part's label and each
    continuation line is indented by two spaces, so a multi-line tool result
    does not lose its structure. A history closing tag inside the content is
    escaped (``_escape_history_tag``, any letter case) so it cannot end the
    block early. Single-line text renders byte-identical to the old collapse,
    so the append-only prefix property holds.
    """
    text = _escape_history_tag(text)
    lines = text.splitlines()
    if len(lines) > 1:
        return "\n  ".join(lines)
    return lines[0] if lines else ""


def _media_line(kind: str, mime_type: str | None) -> str:
    """A media placeholder naming the media type, e.g. ``[image: image/png]``;
    just the kind (``[image]``) when no MIME type is recorded."""
    return f"[{kind}: {mime_type}]" if mime_type else f"[{kind}]"


def _part_lines(
    msg: Message, name_of: Callable[[str], str], call_names: dict[str, str]
) -> list[str]:
    """One rendered entry per part of ``msg``; parts with no rendering are dropped.

    ``call_names`` maps a ToolCallPart id to its tool name and is built
    INCREMENTALLY by the caller as it walks the older messages in order, so a
    tool-result message (which carries no name) renders with the name of the
    latest EARLIER call it answers. A result whose call is not in the map
    renders with its RAW id (not through ``name_of`` -- it is a call id, not a
    tool name).
    """
    lines: list[str] = []
    for part in msg.parts:
        if isinstance(part, TextPart):
            lines.append(f"[{msg.role}] {_content(part.text)}")
        elif isinstance(part, ToolCallPart):
            call_names[part.id] = part.name
            # Spec 6.4 v1.6: the closing tag is escaped in the rendered args
            # too (a write/edit call quoting it must not close the block);
            # \/ is a valid JSON escape, so the args still decode unchanged.
            args = _escape_history_tag(json.dumps(part.arguments, sort_keys=True, ensure_ascii=False))
            lines.append(f"[assistant -> tool call] {name_of(part.name)} {args}")
        elif isinstance(part, ToolResultPart):
            name = call_names.get(part.id)
            label = name_of(name) if name is not None else part.id
            err = " error" if part.error else ""
            lines.append(f"[tool result {label}{err}] {_content(part.output)}")
        elif isinstance(part, ImagePart):
            lines.append(_media_line("image", part.mime_type))
        elif isinstance(part, DocumentPart):
            lines.append(_media_line("document", part.mime_type))
        elif isinstance(part, ExtendedPart):
            kind = "audio" if isinstance(part.extended, AudioPart) else "video"
            lines.append(_media_line(kind, part.extended.mime_type))
        # else: a non-renderable part type (e.g. reasoning) is dropped (spec 6.4).
    return lines


def plan_replay(messages: Sequence[Message], *, name_of: Callable[[str], str]) -> ReplayPlan:
    """Split ``messages`` into the system text (with older history rendered) and
    the structured tail (spec 6.4).

    ``name_of`` turns a tool name into the label used in the rendered lines (a
    provider maps toolset tool names to its own, e.g. ``mcp__<set>__<tool>``).
    The plan retains an empty or assistant-ending tail so the calling adapter
    can validate it before execution.
    """
    older, tail = _partition_history(messages)

    system_text = "\n\n".join(
        "".join(p.text for p in m.parts if isinstance(p, TextPart))
        for m in messages
        if m.role == "system"
    )
    # Walk the older history IN ORDER, building the id-to-name map as we go: a
    # result sees only the latest earlier call with its id, so appending a later
    # round (which may reuse ids) can never rewrite an earlier rendered line.
    call_names: dict[str, str] = {}
    lines = [line for m in older for line in _part_lines(m, name_of, call_names)]
    if lines:
        block = "<conversation_history>\n" + "\n".join(lines) + "\n</conversation_history>"
        system_text = f"{system_text}\n\n{block}" if system_text else block
    return ReplayPlan(system_text=system_text, tail=tail)
