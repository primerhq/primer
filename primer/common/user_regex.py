"""Bounded compilation and matching for regular expressions a user wrote.

Every place the platform runs a pattern that came from a user (workspace
``grep``, collection grep and the regex search mode, the tap ``LIKE``
selector, graph ``output_schema`` patterns, event-filter field regexes and
the channel ``text_pattern``) goes through this module, so the same three
bounds hold everywhere:

* **Size.** A pattern is at most :data:`MAX_PATTERN_LENGTH` characters
  where the user writes it. The ``regex`` engine also unrolls counted
  repeats when it compiles (``(?:a{1000}){1000}`` builds a million nodes,
  ``(?:a{65535}){65535}`` never finishes), so the product of nested
  ``{n}`` / ``{n,m}`` counts is capped at :data:`MAX_REPEAT_EXPANSION`.
  Both are checked BEFORE the compiler runs.
* **Engine.** Patterns compile with the ``regex`` engine (``VERSION0``,
  the stdlib ``re`` dialect) and are cached, so the dialect a pattern is
  validated in is the dialect it is matched in, and a stored pattern
  compiles once per process.
* **Time.** Matching passes ``timeout=`` (the engine raises
  ``TimeoutError``) and ``concurrent=True``, which releases the GIL while
  the engine runs, so a match in a worker thread does not stall the event
  loop the way stdlib ``re`` (which holds the GIL) does. A
  :class:`MatchBudget` shares one total time bound across many searches.

#812 introduced the timed ``regex`` matcher for ``text_pattern``; this is
that helper, generalised (G6-4, G6-5, G6-11).
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import TypeVar

import regex

MAX_PATTERN_LENGTH = 1000
MAX_REPEAT_EXPANSION = 100_000

_COMPILE_CACHE_SIZE = 1024

T = TypeVar("T")


class UserRegexError(ValueError):
    """A user pattern is too long, repeats too much, or does not compile."""


class UserRegexTimeout(TimeoutError):
    """Matching a user pattern ran past its time bound."""

    def __init__(self, message: str, *, seconds: float) -> None:
        super().__init__(message)
        self.seconds = seconds


def _is_count(text: str) -> bool:
    return text.isascii() and text.isdigit()


def _quantifier_at(pattern: str, index: int) -> tuple[int, int] | None:
    """``(count, end)`` for a ``{n}``, ``{n,}``, ``{,m}`` or ``{n,m}`` at ``index``, else ``None``.

    ``count`` is the largest number the quantifier names (the compiler unrolls
    up to it); ``end`` is the index just past the closing brace. Anything else
    in braces (``{e<=2}`` fuzzy constraints, a literal ``{``) is not a count.
    """
    if index >= len(pattern) or pattern[index] != "{":
        return None
    close = pattern.find("}", index + 1)
    if close == -1:
        return None
    low, comma, high = pattern[index + 1 : close].partition(",")
    low, high = low.strip(), high.strip()
    if not (_is_count(low) or (comma and _is_count(high))):
        return None
    if (low and not _is_count(low)) or (high and not _is_count(high)):
        return None
    count = max(int(low) if low else 0, int(high) if high else 0)
    return count, close + 1


def repeat_expansion(pattern: str) -> int:
    """An upper bound on how many nodes the compiler unrolls for ``pattern``.

    Each atom costs 1; a group costs the sum of what it holds (every
    alternative is compiled); a counted quantifier multiplies the cost of
    the atom or group it follows by its largest count. ``*``, ``+`` and
    ``?`` do not unroll. The scan skips escapes and character classes, and
    stops counting once the cost passes :data:`MAX_REPEAT_EXPANSION` (the
    answer is then only known to be over the cap).
    """
    frames: list[int] = [0]
    last = 0
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            frames[-1] += 1
            last = 1
            index += 2
            continue
        if char == "[":
            index += 1
            if index < length and pattern[index] == "^":
                index += 1
            if index < length and pattern[index] == "]":
                index += 1
            while index < length and pattern[index] != "]":
                index += 2 if pattern[index] == "\\" else 1
            frames[-1] += 1
            last = 1
            index += 1
            continue
        if char == "(":
            frames.append(0)
            index += 1
            continue
        if char == ")" and len(frames) > 1:
            last = max(frames.pop(), 1)
            frames[-1] += last
            index += 1
            continue
        quantifier = _quantifier_at(pattern, index)
        if quantifier is not None:
            count, index = quantifier
            if count > 1:
                frames[-1] += last * (count - 1)
                last *= count
            if frames[-1] > MAX_REPEAT_EXPANSION:
                return frames[-1]
            continue
        if char in "*+?|":
            index += 1
            continue
        frames[-1] += 1
        last = 1
        index += 1
    return sum(frames)


def check_user_pattern(pattern: str, *, max_length: int | None = MAX_PATTERN_LENGTH, what: str = "pattern") -> None:
    """Refuse a pattern that is too long or repeats too much, without compiling it."""
    if max_length is not None and len(pattern) > max_length:
        raise UserRegexError(
            f"{what} is {len(pattern)} characters long; the limit is {max_length}"
        )
    expansion = repeat_expansion(pattern)
    if expansion > MAX_REPEAT_EXPANSION:
        raise UserRegexError(
            f"{what} repeats too much to compile (its counted repeats unroll to more than "
            f"{MAX_REPEAT_EXPANSION} steps); use smaller {{n}} counts or * and +"
        )


@functools.lru_cache(maxsize=_COMPILE_CACHE_SIZE)
def _compile(pattern: str, flags: int) -> regex.Pattern:
    return regex.compile(pattern, flags | regex.VERSION0)


def compile_user_regex(
    pattern: str,
    flags: int = 0,
    *,
    max_length: int | None = MAX_PATTERN_LENGTH,
    what: str = "pattern",
) -> regex.Pattern:
    """Check and compile a user pattern with the ``regex`` engine (cached).

    ``flags`` are ``regex`` flags (``regex.IGNORECASE``, ``regex.DOTALL``,
    ``regex.MULTILINE``). Raises :class:`UserRegexError`, whose message
    starts with ``invalid regex`` when the pattern does not compile.
    """
    check_user_pattern(pattern, max_length=max_length, what=what)
    try:
        return _compile(pattern, flags)
    except (regex.error, OverflowError, RecursionError, ValueError) as exc:
        raise UserRegexError(f"invalid regex: {exc}") from None


def timed(call: Callable[[float], T], *, timeout: float, what: str = "pattern") -> T:
    """Run ``call(timeout)`` (a ``regex`` method given ``timeout=``) and turn the engine's timeout into :class:`UserRegexTimeout`."""
    if timeout <= 0:
        raise UserRegexTimeout(f"{what} matching ran out of time", seconds=0.0)
    try:
        return call(timeout)
    except TimeoutError:
        raise UserRegexTimeout(
            f"{what} matching ran past its {timeout:g} s bound", seconds=timeout,
        ) from None


def timed_search(compiled: regex.Pattern, text: str, *, timeout: float) -> regex.Match | None:
    """``compiled.search(text)`` bounded by ``timeout`` seconds, with the GIL released."""
    return timed(lambda t: compiled.search(text, timeout=t, concurrent=True), timeout=timeout)


class MatchBudget:
    """One total time bound shared by many matches.

    The budget counts only the time spent inside the engine, so the file
    reads and walks around a grep do not use it up. Each call runs with a
    timeout of what is left (or ``per_search``, when smaller) and raises
    :class:`UserRegexTimeout` when the engine stops it or nothing is left;
    :attr:`exhausted` then says whether the whole budget ran out. Not
    thread-safe: use one budget from one thread at a time (a sequence of
    ``asyncio.to_thread`` calls is fine).
    """

    def __init__(self, seconds: float, *, per_search: float | None = None, what: str = "pattern") -> None:
        self.seconds = seconds
        self.per_search = per_search
        self.what = what
        self.spent = 0.0

    @property
    def remaining(self) -> float:
        return max(self.seconds - self.spent, 0.0)

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.seconds

    def _run(self, call: Callable[[float], T]) -> T:
        remaining = self.seconds - self.spent
        if remaining <= 0:
            raise UserRegexTimeout(
                f"{self.what} matching used its {self.seconds:g} s budget", seconds=self.seconds,
            )
        bound = remaining if self.per_search is None else min(remaining, self.per_search)
        started = time.perf_counter()
        try:
            return timed(call, timeout=bound, what=self.what)
        except UserRegexTimeout:
            if bound >= remaining:
                self.spent = self.seconds
                raise UserRegexTimeout(
                    f"{self.what} matching used its {self.seconds:g} s budget", seconds=self.seconds,
                ) from None
            raise
        finally:
            if self.spent < self.seconds:
                self.spent = min(self.spent + time.perf_counter() - started, self.seconds)

    def search(self, compiled: regex.Pattern, text: str) -> regex.Match | None:
        return self._run(lambda t: compiled.search(text, timeout=t, concurrent=True))

    def match(self, compiled: regex.Pattern, text: str) -> regex.Match | None:
        return self._run(lambda t: compiled.match(text, timeout=t, concurrent=True))

    def findall_matches(self, compiled: regex.Pattern, text: str) -> list[regex.Match]:
        """Every match of ``compiled`` in ``text`` (``finditer``, materialised under ONE timeout)."""
        return self._run(lambda t: list(compiled.finditer(text, timeout=t, concurrent=True)))


__all__ = [
    "MAX_PATTERN_LENGTH",
    "MAX_REPEAT_EXPANSION",
    "MatchBudget",
    "UserRegexError",
    "UserRegexTimeout",
    "check_user_pattern",
    "compile_user_regex",
    "repeat_expansion",
    "timed",
    "timed_search",
]
