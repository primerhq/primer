from __future__ import annotations

import re

import regex
from pydantic import BaseModel

from primer.model.channel_event import ChannelEvent, NormalizedEventType


# A channel event is matched on the shared inbound path. The stdlib ``re``
# engine has no execution budget, so an already-stored pattern must never be
# allowed to monopolise that path. Ten milliseconds is generous for the
# short message bodies handled here while keeping a hostile pattern bounded.
_TEXT_PATTERN_TIMEOUT_SECONDS = 0.01


def _unbounded_repeat_at(pattern: str, index: int) -> bool:
    """Return whether an unescaped quantifier at *index* has no upper bound."""
    if index >= len(pattern):
        return False
    if pattern[index] in "*+":
        return True
    if pattern[index] != "{":
        return False
    close = pattern.find("}", index + 1)
    if close == -1:
        return False
    bounds = pattern[index + 1 : close]
    minimum, comma, maximum = bounds.partition(",")
    return bool(comma and minimum.isdigit() and not maximum)


def _contains_nested_unbounded_repeat(pattern: str) -> bool:
    """Return whether *pattern* repeats a group containing an open repeat.

    Nested backtracking repeats such as ``(a+)+`` are a common catastrophic
    backtracking shape. This small scanner ignores escaped metacharacters and
    character classes, propagating whether each open group contains an
    unbounded repeat. The timed matcher below remains the backstop for other
    costly shapes and for patterns stored before this validator existed.
    """
    groups = [False]
    in_character_class = False
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            index += 2
            continue
        if in_character_class:
            if character == "]":
                in_character_class = False
            index += 1
            continue
        if character == "[":
            in_character_class = True
        elif character == "(":
            groups.append(False)
        elif character == ")" and len(groups) > 1:
            contains_repeat = groups.pop()
            if contains_repeat and _unbounded_repeat_at(pattern, index + 1):
                return True
            groups[-1] = groups[-1] or contains_repeat
        elif _unbounded_repeat_at(pattern, index):
            groups[-1] = True
        index += 1
    return False


class EventMatcherPatternError(ValueError):
    """An EventMatcher text pattern is invalid or unsafe to persist."""


def validate_text_pattern(pattern: str | None) -> None:
    """Reject malformed patterns and the common nested-repeat ReDoS shape."""
    if pattern is None:
        return
    try:
        re.compile(pattern)
    except re.error as exc:
        raise EventMatcherPatternError(
            "text_pattern must be a valid regular expression"
        ) from exc
    if _contains_nested_unbounded_repeat(pattern):
        raise EventMatcherPatternError(
            "text_pattern must not contain nested unbounded repetition"
        )


class EventMatcher(BaseModel):
    event_type: NormalizedEventType
    surface: list[str] | None = None
    room_external_ids: list[str] | None = None
    command_name: str | None = None
    mentions_bot: bool | None = None
    sender_roles_any: list[str] | None = None
    sender_ids_any: list[str] | None = None
    text_pattern: str | None = None


def matches(matcher: EventMatcher, event: ChannelEvent) -> bool:
    if event.type != matcher.event_type:
        return False
    if matcher.surface is not None and event.surface not in matcher.surface:
        return False
    if (
        matcher.room_external_ids is not None
        and event.room_external_id not in matcher.room_external_ids
    ):
        return False
    if matcher.command_name is not None and not (
        event.command is not None
        and event.command.get("name") == matcher.command_name
    ):
        return False
    if matcher.mentions_bot is not None and event.mentions_bot != matcher.mentions_bot:
        return False
    if matcher.sender_roles_any is not None and not (
        set(event.sender.roles) & set(matcher.sender_roles_any)
    ):
        return False
    if (
        matcher.sender_ids_any is not None
        and event.sender.external_id not in matcher.sender_ids_any
    ):
        return False
    if matcher.text_pattern is not None:
        if event.text is None:
            return False
        try:
            text_match = regex.search(
                matcher.text_pattern,
                event.text,
                flags=regex.VERSION0,
                timeout=_TEXT_PATTERN_TIMEOUT_SECONDS,
            )
        except (TimeoutError, regex.error):
            # Timeout means "no match", not "stall every inbound event".
            # regex.error covers a malformed legacy row that predates model
            # validation, keeping the same fail-closed behaviour.
            return False
        if text_match is None:
            return False
    return True


__all__ = [
    "EventMatcher",
    "EventMatcherPatternError",
    "matches",
    "validate_text_pattern",
]
