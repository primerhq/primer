"""Size caps on the free-text fields a USER writes into a row (D21, GI-26).

Only the 32 MiB request-body cap bounded a description or a prompt, and a public row's description is copied into the live map, carried by every CRUD
event and re-embedded. The caps are generous and apply at the user write doors only (the REST routes and the system tools); internal writers do not
pass through them, and a save that leaves an over-cap field unchanged still passes, so a row stored before the caps stays editable elsewhere.
"""

from __future__ import annotations

from typing import Any

from primer.common.entity_checks import EntityCheckError

DESCRIPTION_MAX_CHARS = 16 * 1024
NAME_MAX_CHARS = 256
PROMPT_MAX_CHARS = 256 * 1024

# field name -> cap. A list field (a prompt kept as parts) counts the sum of its parts.
CAPS: dict[str, int] = {
    "description": DESCRIPTION_MAX_CHARS,
    "name": NAME_MAX_CHARS,
    "system_prompt": PROMPT_MAX_CHARS,
    "compaction_prompt": PROMPT_MAX_CHARS,
}


def _size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(len(part) for part in value if isinstance(part, str))
    return 0


def oversized_fields(entity: Any, existing: Any | None = None) -> list[tuple[str, int, int]]:
    """``(field, cap, size)`` for every capped field of ``entity`` over its cap, leaving out one equal to the stored row's (an unchanged field passes)."""
    over: list[tuple[str, int, int]] = []
    for field, cap in CAPS.items():
        value = getattr(entity, field, None)
        size = _size(value)
        if size <= cap:
            continue
        if existing is not None and getattr(existing, field, None) == value:
            continue
        over.append((field, cap, size))
    return over


def check_text_caps(entity: Any, existing: Any | None = None) -> None:
    """Raise a validation :class:`EntityCheckError` naming the first over-cap field."""
    over = oversized_fields(entity, existing)
    if over:
        field, cap, size = over[0]
        raise EntityCheckError(
            "validation", f"{field} is too long: {size} characters, at most {cap} are accepted", field=field,
        )


__all__ = ["CAPS", "DESCRIPTION_MAX_CHARS", "NAME_MAX_CHARS", "PROMPT_MAX_CHARS", "check_text_caps", "oversized_fields"]
