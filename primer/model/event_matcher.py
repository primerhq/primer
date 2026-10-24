from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel

from primer.common.user_regex import (
    MAX_PATTERN_LENGTH,
    MatchBudget,
    UserRegexError,
    UserRegexTimeout,
    compile_user_regex,
)
from primer.model.channel_event import ChannelEvent, NormalizedEventType

logger = logging.getLogger(__name__)

# A channel event is matched on the shared inbound path. The stdlib ``re``
# engine has no execution budget, so an already-stored pattern must never be
# allowed to monopolise that path. Ten milliseconds is generous for the
# short message bodies handled here while keeping a hostile pattern bounded.
_TEXT_PATTERN_TIMEOUT_SECONDS = 0.01
# ...and the 10 ms is per search, so one event matched against many
# subscriptions adds them up (50 hostile subscriptions cost 0.5 s per event).
# Every text_pattern search and compile one channel event runs, across every
# subscription of every trigger it fires, shares this total (G6-5), split
# equally between the subscriptions' distinct owners (round 2, M1).
_EVENT_TEXT_BUDGET_SECONDS = 0.1
# The longest text_pattern a subscription may store (G6-5).
TEXT_PATTERN_MAX_LENGTH = MAX_PATTERN_LENGTH


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


def _compile_text_pattern(pattern: str):
    """The matcher's compile: the same engine and checks as the save path, cached with its verdict (refusals too)."""
    return compile_user_regex(pattern, max_length=TEXT_PATTERN_MAX_LENGTH, what="text_pattern", stored=True)


def validate_text_pattern(pattern: str | None) -> None:
    """Reject malformed, oversized and costly patterns before they are stored.

    The pattern passes the same checks and is compiled with the engine that
    matches it (``regex``, ``VERSION0``), so the save-time and match-time
    dialects agree. The checks run first: at most
    :data:`TEXT_PATTERN_MAX_LENGTH` characters, the refused constructs, and
    the expansion cap read from the engine's parse tree. Only the matcher
    caches a compile and its verdict.
    """
    if pattern is None:
        return
    try:
        # The save path checks and compiles but never touches the verdict
        # cache (#859 round 3, R2-8): only the match path fills it.
        compile_user_regex(pattern, max_length=TEXT_PATTERN_MAX_LENGTH, what="text_pattern")
    except UserRegexError as exc:
        if str(exc).startswith("invalid regex"):
            raise EventMatcherPatternError(
                "text_pattern must be a valid regular expression"
            ) from exc
        raise EventMatcherPatternError(str(exc)) from None
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


class EventTextBudget(MatchBudget):
    """The text_pattern budget of one channel event: :data:`_EVENT_TEXT_BUDGET_SECONDS` in total, 10 ms per search.

    ``owner_keys`` maps each text_pattern subscription the event will be
    matched against to its owner (``primer.trigger.dispatch.owner_keys``:
    an API token counts as its user, a row with no owner as itself), and
    ``owners`` is the count to use without them. :meth:`for_owner` gives
    each owner an equal share that draws on this pool, so one owner's slow
    subscriptions cannot starve another owner's (a share left unused is not
    handed to anyone else, and a share is charged to the pool at most what
    it had left). ``subscriptions`` carries what the router read, by
    trigger id, so ``fire_trigger`` does not read it again.
    """

    def __init__(
        self,
        owners: int = 1,
        *,
        owner_keys: dict[str, Hashable] | None = None,
        subscriptions: dict[str, list] | None = None,
    ) -> None:
        super().__init__(
            _EVENT_TEXT_BUDGET_SECONDS, per_search=_TEXT_PATTERN_TIMEOUT_SECONDS, what="text_pattern",
        )
        self.owner_keys = dict(owner_keys or {})
        self.owner_count = max(len(set(self.owner_keys.values())) if self.owner_keys else owners, 1)
        self.subscriptions = subscriptions
        self._owner_budgets: dict[Hashable, MatchBudget] = {}
        self.warned = False

    def subscriptions_for(self, trigger_id: str) -> list | None:
        """The enabled subscriptions the router read for ``trigger_id``, or None when it read none."""
        return None if self.subscriptions is None else self.subscriptions.get(trigger_id)

    def for_subscription(self, subscription_id: str) -> MatchBudget:
        """The share of the subscription's owner (a subscription the router did not see is its own owner)."""
        return self.for_owner(self.owner_keys.get(subscription_id, ("subscription", subscription_id)))

    def for_owner(self, owner: Hashable) -> MatchBudget:
        budget = self._owner_budgets.get(owner)
        if budget is None:
            budget = MatchBudget(
                self.seconds / self.owner_count, per_search=_TEXT_PATTERN_TIMEOUT_SECONDS,
                what="text_pattern", parent=self,
            )
            self._owner_budgets[owner] = budget
        return budget


_EVENT_TEXT_BUDGET: ContextVar[EventTextBudget | None] = ContextVar(
    "primer_channel_event_text_budget", default=None,
)


@contextmanager
def channel_event_text_budget(
    owners: int = 1,
    *,
    owner_keys: dict[str, Hashable] | None = None,
    subscriptions: dict[str, list] | None = None,
) -> Iterator[EventTextBudget]:
    """Share one text_pattern budget across everything one channel event fires.

    ``ChannelEventRouter`` opens it around its trigger fan-out, with the
    subscriptions it read and the owner of each text_pattern subscription;
    ``fire_trigger`` reads it with :func:`current_event_text_budget` (and
    opens its own for a fire outside one).
    """
    budget = EventTextBudget(owners, owner_keys=owner_keys, subscriptions=subscriptions)
    token = _EVENT_TEXT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _EVENT_TEXT_BUDGET.reset(token)


def current_event_text_budget() -> EventTextBudget | None:
    return _EVENT_TEXT_BUDGET.get()


MatchOutcome = Literal["match", "no_match", "budget_exhausted"]

# Digests of stored patterns the matcher refused to compile, so each is logged once per process.
_REFUSED_LOGGED: set[str] = set()


def _log_refused(pattern: str, exc: Exception) -> None:
    digest = hashlib.sha256(pattern.encode("utf-8")).hexdigest()
    if digest not in _REFUSED_LOGGED and len(_REFUSED_LOGGED) < 1024:
        _REFUSED_LOGGED.add(digest)
        logger.warning("channel event: a stored text_pattern never matches (%s)", exc)


def match_outcome(
    matcher: EventMatcher, event: ChannelEvent, *, budget: MatchBudget | None = None,
) -> MatchOutcome:
    """Whether ``event`` matches, and when not, whether the text budget is why.

    ``budget_exhausted`` means the text_pattern search was cut because the
    WHOLE ``budget`` ran out; a search that only hit its own 10 ms bound is a
    plain ``no_match`` (#812). Without a ``budget`` a fresh one is used.
    """
    return _outcome(matcher, event, budget)


def matches(matcher: EventMatcher, event: ChannelEvent, *, budget: MatchBudget | None = None) -> bool:
    return _outcome(matcher, event, budget) == "match"


def _outcome(matcher: EventMatcher, event: ChannelEvent, budget: MatchBudget | None) -> MatchOutcome:
    if event.type != matcher.event_type:
        return "no_match"
    if not _fields_hold(matcher, event):
        return "no_match"
    if matcher.text_pattern is None:
        return "match"
    if event.text is None:
        return "no_match"
    bound = budget if budget is not None else EventTextBudget()
    if bound.exhausted:
        # Spent: answer before compiling anything (round 2, B3).
        return "budget_exhausted"
    started = time.perf_counter()
    try:
        # Compiled once per process (the cache validate_text_pattern filled),
        # and a refusal is cached too. A legacy row that predates validation,
        # or the caps, fails closed here instead of being checked again.
        compiled = _compile_text_pattern(matcher.text_pattern)
    except UserRegexError as exc:
        _log_refused(matcher.text_pattern, exc)
        return "no_match"
    finally:
        # A compile is work done for this event: it is charged like a search.
        bound.charge(time.perf_counter() - started)
    if bound.exhausted:
        return "budget_exhausted"
    try:
        text_match = bound.search(compiled, event.text)
    except UserRegexTimeout:
        # Timeout means "no match", not "stall every inbound event".
        return "budget_exhausted" if bound.exhausted else "no_match"
    return "match" if text_match is not None else "no_match"


def _fields_hold(matcher: EventMatcher, event: ChannelEvent) -> bool:
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
    return True


__all__ = [
    "EventMatcher",
    "EventMatcherPatternError",
    "EventTextBudget",
    "MatchOutcome",
    "TEXT_PATTERN_MAX_LENGTH",
    "channel_event_text_budget",
    "current_event_text_budget",
    "match_outcome",
    "matches",
    "validate_text_pattern",
]
