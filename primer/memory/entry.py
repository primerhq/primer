"""The body of a memory entry: markdown sections of ``- [id] text`` bullets.

``## Steps``, ``## Pitfalls`` and ``## Example``; item ids are ``s``, ``p`` and ``e`` plus a number, so a patch can name one bullet. A section with no
items is left out of the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from primer.model.memory import ItemEdit, MemoryAdd

#: The longest rendered body (about 800 tokens).
BODY_MAX_CHARS = 3200
MEMORY_FOLDERS = ("active", "candidates", "archived")

_SECTIONS = (("steps", "Steps", "s"), ("pitfalls", "Pitfalls", "p"), ("example", "Example", "e"))
_BULLET = re.compile(r"^- \[([spe]\d+)\] (.*)$")


@dataclass
class EntryBody:
    steps: list[tuple[str, str]] = field(default_factory=list)
    pitfalls: list[tuple[str, str]] = field(default_factory=list)
    example: list[tuple[str, str]] = field(default_factory=list)


def render_body(body: EntryBody) -> str:
    parts = []
    for attr, title, _ in _SECTIONS:
        items = getattr(body, attr)
        if items:
            parts.append(f"## {title}\n" + "\n".join(f"- [{item_id}] {' '.join(text.split())}" for item_id, text in items))
    text = "\n\n".join(parts) + "\n" if parts else ""
    if len(text) > BODY_MAX_CHARS:
        raise ValueError(f"a memory entry body is at most {BODY_MAX_CHARS} characters; this one is {len(text)}")
    return text


def parse_body(text: str) -> EntryBody:
    body = EntryBody()
    current: list[tuple[str, str]] | None = None
    for line in text.splitlines():
        heading = line.strip().removeprefix("## ") if line.startswith("## ") else None
        if heading is not None:
            current = next((getattr(body, attr) for attr, title, _ in _SECTIONS if title == heading), None)
            continue
        m = _BULLET.match(line)
        if m and current is not None:
            current.append((m.group(1), m.group(2)))
    return body


def body_from_add(add: MemoryAdd) -> EntryBody:
    return EntryBody(
        steps=[(f"s{i}", t) for i, t in enumerate(add.steps, 1)],
        pitfalls=[(f"p{i}", t) for i, t in enumerate(add.pitfalls, 1)],
        example=[("e1", add.example)] if add.example else [],
    )


def apply_edits(body: EntryBody, edits: list[ItemEdit]) -> EntryBody:
    """``body`` with the edits applied, by item id. An edit naming an unknown id, or an add/change with no text, is ignored. The input is not changed."""
    out = EntryBody(steps=list(body.steps), pitfalls=list(body.pitfalls), example=list(body.example))
    for edit in edits:
        items: list[tuple[str, str]] = getattr(out, edit.section)
        prefix = next(p for attr, _, p in _SECTIONS if attr == edit.section)
        if edit.action == "add":
            if edit.text:
                top = max((int(i[1:]) for i, _ in items), default=0)
                items.append((f"{prefix}{top + 1}", edit.text))
            continue
        at = next((n for n, (i, _) in enumerate(items) if i == edit.item_id), None)
        if at is None:
            continue
        if edit.action == "remove":
            del items[at]
        elif edit.text:
            items[at] = (items[at][0], edit.text)
    return out


def slugify_name(name: str) -> str | None:
    """``name`` as a ``[a-z0-9-]`` slug, or None when nothing is left."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or None


__all__ = [
    "BODY_MAX_CHARS", "EntryBody", "MEMORY_FOLDERS", "apply_edits", "body_from_add", "parse_body", "render_body", "slugify_name",
]
