"""Google-style docstring to the description anatomy make_tool enforces.

A python tool's description lands in the same LLM context as every built-in
tool, so it is held to the same bar: one imperative purpose sentence, a
"use when" clause, and a documented entry per argument. Enforcing that at
registration means a bad docstring fails when the operator saves, not when an
agent is mid-turn and the only signal is a confused model.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from typing import Any

_SECTIONS = ("args:", "arguments:", "when:", "examples:", "returns:", "raises:")

# An arg description continued on the next line is indented past the name.
# Google style puts names at 4 and continuations at 8 relative to the block,
# which after dedent lands at 8 and 12 respectively.
_CONTINUATION_INDENT = 12


class DocstringError(ValueError):
    """A docstring that cannot produce a usable tool description.

    ``field`` names the offending part so the API can point the operator at
    it rather than returning "invalid docstring".
    """

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass
class ParsedDocstring:
    purpose: str
    when: str
    args: dict[str, str] = field(default_factory=dict)
    examples: list[dict[str, Any]] = field(default_factory=list)


def _split_sections(body: str) -> tuple[str, dict[str, list[str]]]:
    """Split a dedented docstring into its head and its named sections."""
    head: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.lower() in _SECTIONS:
            current = stripped.lower().rstrip(":")
            sections[current] = []
            continue
        if current is None:
            head.append(raw)
        else:
            sections[current].append(raw)
    return "\n".join(head), sections


def _parse_args(lines: list[str]) -> dict[str, str]:
    """Parse an ``Args:`` block into {name: description}.

    A line indented past the name continues the previous description, which is
    how Google style wraps a long one.
    """
    out: dict[str, str] = {}
    name: str | None = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip())
        is_continuation = name is not None and indent >= _CONTINUATION_INDENT
        if ":" in stripped and not is_continuation:
            key, _, desc = stripped.partition(":")
            # "name (str)" -> "name"; the type comes from the annotation, not
            # from prose that can disagree with it.
            name = key.split("(")[0].strip()
            out[name] = desc.strip()
        elif name is not None:
            out[name] = (out[name] + " " + stripped).strip()
    return out


def parse_docstring(text: str) -> ParsedDocstring:
    """Parse ``text`` into the anatomy make_tool needs.

    Raises :class:`DocstringError` naming the offending field.
    """
    if not text or not text.strip():
        raise DocstringError("the function needs a docstring", field="docstring")

    body = textwrap.dedent(text.strip("\n"))
    head, sections = _split_sections(body)

    head_lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    if not head_lines:
        raise DocstringError("the docstring needs a summary line", field="purpose")
    purpose = head_lines[0]

    when = ""
    for line in head_lines[1:]:
        if line.lower().startswith("use when"):
            when = line
            break
    if not when and "when" in sections:
        joined = " ".join(x.strip() for x in sections["when"] if x.strip())
        if joined:
            when = f"Use when {joined}"
    if not when:
        raise DocstringError(
            "the docstring needs a 'Use when ...' line or a 'When:' section",
            field="when",
        )

    arg_lines = sections.get("args") or sections.get("arguments") or []
    args = _parse_args(arg_lines)

    examples: list[dict[str, Any]] = []
    for raw in sections.get("examples", []):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DocstringError(
                f"example is not valid JSON: {stripped!r}", field="examples"
            ) from exc
        if not isinstance(parsed, dict):
            raise DocstringError(
                "each example must be a JSON object of arguments", field="examples"
            )
        examples.append(parsed)

    return ParsedDocstring(purpose=purpose, when=when, args=args, examples=examples)
