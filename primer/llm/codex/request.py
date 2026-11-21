"""Bounded stateless replay, with only application-owned function declarations."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from primer.llm.codex.boundary import BoundaryError
from primer.model.chat import Message, TextPart, Tool, ToolCallPart, ToolResultPart
from primer.model.except_ import UnsupportedContentError


def compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def decode(value: str | bytes) -> object:
    """Reject ambiguous objects and non-JSON numeric constants at the boundary."""
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = item
        return result

    def constant(_):
        raise ValueError("non-JSON numeric constant")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def alias(name: str) -> str:
    return "primer_" + hashlib.sha256(name.encode()).hexdigest()[:48]


@dataclass(frozen=True)
class ModelRequest:
    # Serialized once to keep mutable caller/tool schemas out of an active step.
    payload: str
    aliases: tuple[tuple[str, str], ...]

    def wire(self) -> dict:
        return decode(self.payload)


def prepare_request(model: str, messages: list[Message], tools: list[Tool] | None = None,
                    *, tool_choice: str | None = None, max_output_tokens: int | None = None,
                    response_schema: dict | None = None) -> ModelRequest:
    if not isinstance(model, str) or not model.strip():
        raise BoundaryError("A model must be selected")
    names = [t.id for t in tools or []]
    if len(names) != len(set(names)):
        raise BoundaryError("Duplicate Primer tool name")
    pairs = tuple((alias(name), name) for name in names)
    if len({a for a, _ in pairs}) != len(pairs):
        raise BoundaryError("Primer tool alias collision")
    # Historical tools may no longer be advertised; their opaque labels still
    # pair exactly with their outputs without re-enabling those tools.
    items: list[dict] = []
    calls: set[str] = set()
    pending: set[str] = set()
    for message in messages:
        for part in message.parts:
            if isinstance(part, TextPart):
                if message.role not in {"system", "developer", "user", "assistant"}:
                    raise BoundaryError("Unsupported text replay role")
                items.append({"type": "message", "role": "developer" if message.role == "system" else message.role,
                              "content": [{"type": "output_text" if message.role == "assistant" else "input_text",
                                           "text": part.text}]})
            elif isinstance(part, ToolCallPart):
                if message.role != "assistant" or part.id in calls:
                    raise BoundaryError("Invalid or duplicate replay call")
                calls.add(part.id)
                pending.add(part.id)
                items.append({"type": "function_call", "call_id": part.id,
                              "namespace": "primer", "name": alias(part.name), "arguments": compact(part.arguments)})
            elif isinstance(part, ToolResultPart):
                if message.role != "tool" or part.id not in pending:
                    raise BoundaryError("Unpaired or duplicate replay result")
                pending.remove(part.id)
                items.append({"type": "function_call_output", "call_id": part.id, "output": part.output})
            else:
                raise UnsupportedContentError("Codex foundation currently accepts text and paired tool history only")
    if pending:
        raise BoundaryError("Unanswered replay call")
    declarations = [{"type": "function", "name": alias(t.id),
                     "description": t.description or "", "parameters": t.args_schema} for t in tools or []]
    wire: dict = {"model": model, "input": items, "tools": ([{"type": "namespace", "name": "primer", "tools": declarations}] if declarations else []), "store": False, "stream": True}
    choice = tool_choice or "auto"
    if choice in {"auto", "none", "required"}:
        if choice == "required" and not names:
            raise BoundaryError("Required tool choice has no tools")
        wire["tool_choice"] = choice
    elif choice in names:
        wire["tool_choice"] = {"type": "function", "namespace": "primer", "name": alias(choice)}
    else:
        raise BoundaryError("Unknown required tool")
    if max_output_tokens is not None:
        raise UnsupportedContentError("ChatGPT plan Responses currently does not accept max_output_tokens")
    if response_schema is not None:
        wire["text"] = {"format": {"type": "json_schema", "name": "primer_output", "schema": response_schema, "strict": True}}
    return ModelRequest(compact(wire), pairs)
