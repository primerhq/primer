"""Bounded compilation and matching for regular expressions a user wrote.

Every place the platform runs a pattern that came from a user (workspace
``grep``, collection grep and the regex search mode, the tap ``LIKE``
selector, graph ``output_schema`` patterns, event-filter field regexes and
the channel ``text_pattern``) goes through this module, so the same bounds
hold everywhere (G6-4, G6-5, G6-11; #812 introduced the timed matcher):

* **Size, decided by the engine's own parse.** A pattern is at most
  :data:`MAX_PATTERN_LENGTH` characters. The ``regex`` engine unrolls
  counted repeats when it compiles (``(?:a{1000}){1000}`` builds a million
  nodes; ``(?:a{65535}){65535}`` exhausts memory), so before anything is
  compiled the pattern is PARSED by the engine's own parser
  (``regex._regex_core``: ``Source``, ``Info``, ``_parse_pattern``, which
  never unrolls) and the parse tree is walked: a repeat multiplies by its
  largest finite count, alternatives and sequences add, and the leaves that
  compile to more code weigh more (``\\X`` 8, a set 1 per property in it).
  ``tests/common/test_user_regex_cost.py`` compiles near-cap shapes in a
  capped child, so a ``regex`` release that changes per-node cost fails a
  test. A pattern whose
  tree costs more than :data:`MAX_REPEAT_EXPANSION` is refused. Because
  the count comes from the tree the engine compiles, no spelling (a comment
  group, verbose whitespace, an inline flag) can hide a repeat from it.
  The parser is a private API, so this module refuses to import when the
  names it uses are gone, and ``tests/common/test_user_regex.py`` proves
  the walker sees every shape the review found.
* **What a search box never needs is refused before parsing:** comment
  groups ``(?#...)``, the verbose flag, inline flag groups anywhere but one
  ``(?aimsu)`` group at the very start, group calls and recursion, and
  (from the tree) fuzzy-matching constraints such as ``{e<=2}``.
* **Engine and memory.** Patterns compile with the ``regex`` engine
  (``VERSION0``, the stdlib ``re`` dialect; never into the engine's own
  cache). A pattern the platform stores (a subscription's ``text_pattern``,
  a filter's field regex, an ``output_schema`` pattern) is kept, on its
  MATCH path only, in a cache bounded by the total expansion it holds,
  together with the check's verdict, refusals included; a save-time check
  and a one-shot pattern (grep, collection grep, the tap selector) never
  touch the cache. ``MemoryError`` and ``RecursionError`` at compile are
  refusals like an over-cap pattern, and are never cached.
* **Time.** Matching passes ``timeout=`` (the engine raises
  ``TimeoutError``) and ``concurrent=True``, which releases the GIL while
  the engine runs. A :class:`MatchBudget` shares one total across many
  searches (and may draw on a parent budget, charging it at most its own
  remaining share); bounded work that must leave the event loop runs on
  one of two small executors, :func:`run_interactive` (tap, output_schema)
  and :func:`run_bulk` (grep), never the default ``to_thread`` pool.
"""

from __future__ import annotations

import asyncio
import bisect
import contextvars
import functools
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

import regex

try:  # The engine's own parser: a private API, pinned here and in the tests.
    from regex import _regex_core as _core

    _PARSE_SOURCE = _core.Source
    _PARSE_INFO = _core.Info
    _PARSE_PATTERN = _core._parse_pattern
    _UNSCOPED_FLAG_SET = _core._UnscopedFlagSet
    _REPEATS = (_core.GreedyRepeat,)  # LazyRepeat and PossessiveRepeat subclass it
    _POSSESSIVE = _core.PossessiveRepeat
    _LEAVES = (
        _core.Character, _core.SetBase, _core.Any, _core.Property, _core.Range,
        _core.RefGroup, _core.Grapheme, _core.ZeroWidthBase,
    )
    _PASS_THROUGH = (_core.Group, _core.Atomic, _core.LookAround)
    _REFUSED_NODES = {
        _core.Fuzzy: "fuzzy matching ({e<=n} and the like) is not allowed",
        _core.CallGroup: "group calls and recursion are not allowed",
        _core.CallRef: "group calls and recursion are not allowed",
        _core.StringSet: "named lists are not allowed",
    }
    _LINE_SAFE_ZERO_WIDTH = (
        _core.StartOfLine, _core.EndOfLine, _core.Boundary, _core.DefaultBoundary,
        _core.StartOfWord, _core.EndOfWord, _core.DefaultStartOfWord, _core.DefaultEndOfWord,
    )
    _SEQUENCE, _BRANCH, _STRING = _core.Sequence, _core.Branch, _core.String
    _CONDITIONAL, _LOOKAROUND_CONDITIONAL = _core.Conditional, _core.LookAroundConditional
except (ImportError, AttributeError) as exc:  # pragma: no cover - exercised only by a regex upgrade
    raise RuntimeError(
        "regex._regex_core no longer provides the parser API primer.common.user_regex walks "
        "(G6-4/G6-5 round 2); the user-regex bounds need a new hook before this regex release can be used"
    ) from exc

MAX_PATTERN_LENGTH = 1000
MAX_REPEAT_EXPANSION = 10_000

# The stored-pattern cache holds at most this much total expansion, and this
# many entries. Measured worst case (a fresh process, RSS after one compile):
# about 290 bytes per expansion unit for a plain near-cap pattern such as
# (?:a{100}){100}, and about the same per WEIGHTED unit for the costly leaves
# the walk weights up (\X, sets of several properties). So the bound holds
# about 30 MB of compiled code at worst, and much less in practice: a typical
# stored pattern weighs a few dozen units.
_CACHE_EXPANSION_BUDGET = 100_000
_CACHE_MAX_ENTRIES = 2048
# Leaves that compile to far more code than one node (measured, see above):
# \X compiles to about 4.4x the memory and 7x the time of a plain character.
_GRAPHEME_WEIGHT = 8

# The global inline-flag letters a pattern may set, in one group at the very start.
_START_FLAGS = frozenset("aimsu")

T = TypeVar("T")


class UserRegexError(ValueError):
    """A user pattern is too long, repeats too much, uses a refused construct, or does not compile."""


class UserRegexTimeout(TimeoutError):
    """Matching a user pattern ran past its time bound.

    ``cause`` is ``"budget"`` when the whole budget ran out and
    ``"per_search"`` when one search hit its own bound.
    """

    def __init__(self, message: str, *, seconds: float, cause: str = "budget") -> None:
        super().__init__(message)
        self.seconds = seconds
        self.cause = cause


# --------------------------------------------------------------------------- the lexical refusals


def _refuse_constructs(pattern: str, what: str) -> None:
    """Refuse comment groups, verbose mode, mid-pattern inline flags and group calls, before the parser runs.

    The scan only has to tell code from escapes and character sets. Without
    comments and verbose mode (both refused here) that is all the dialect has,
    and where it misreads a set it reads MORE of the pattern as code, so it
    can refuse a little too much but never accept one of these constructs.
    """
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
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
            index += 1
            continue
        if char == "(" and pattern.startswith("(?", index):
            _check_extension(pattern, index, what)
        index += 1


def _check_extension(pattern: str, index: int, what: str) -> None:
    rest = pattern[index + 2:]
    if rest.startswith("#"):
        raise UserRegexError(f"{what} may not contain comment groups (?#...)")
    if rest[:1] in (":", "=", "!", ">", "|", "(") or rest.startswith(("<=", "<!", "P<", "P=")):
        return
    if rest[:1] == "<" and rest[1:2] and (rest[1].isalpha() or rest[1] == "_"):
        return  # a named group (?<name>...)
    calls = (
        rest[:1] in ("&", "R")
        or rest[:1].isdigit()
        or rest.startswith("P>")
        or (rest[:1] in ("+", "-") and rest[1:2].isdigit())
    )
    if calls:
        raise UserRegexError(f"{what} may not use group calls or recursion")
    if rest[:1] == "'":
        raise UserRegexError(f"{what} may not use (?'name'...) named groups; write (?P<name>...) or (?<name>...)")
    if not (rest[:1].isalpha() or rest[:1] in "-^"):
        raise UserRegexError(f"{what} uses a group construct that is not supported: (?{rest[:1]}")
    flags_end = 0
    while flags_end < len(rest) and (rest[flags_end].isalnum() or rest[flags_end] in "-^"):
        flags_end += 1
    flags, closer = rest[:flags_end], rest[flags_end:flags_end + 1]
    if "x" in flags or "X" in flags:
        raise UserRegexError(f"{what} may not use verbose mode (the x flag)")
    if closer == ")" and index == 0 and flags and set(flags) <= _START_FLAGS:
        return
    raise UserRegexError(
        f"{what} may set inline flags only in one (?aimsu) group at the very start "
        "(no scoped or mid-pattern flag groups)"
    )


# --------------------------------------------------------------------------- the engine's parse


def _parse(pattern: str, flags: int) -> Any:
    """The engine's parse tree for ``pattern`` (VERSION0), exactly as ``regex.compile`` would build it before compiling."""
    global_flags = flags | regex.VERSION0
    for _attempt in range(3):
        source = _PARSE_SOURCE(pattern)
        info = _PARSE_INFO(global_flags, source.char_type, {})
        info.guess_encoding = regex.UNICODE
        source.ignore_space = bool(info.flags & regex.VERBOSE)
        try:
            parsed = _PARSE_PATTERN(source, info)
        except _UNSCOPED_FLAG_SET:
            # A global inline flag: the engine re-parses with it set from the start.
            global_flags = info.global_flags
            continue
        if not source.at_end():
            raise regex.error("unbalanced parenthesis", pattern, source.pos)
        return parsed
    raise regex.error("the inline flags do not settle", pattern, 0)


def _expansion(node: Any, what: str) -> int:
    """How many nodes the compiler unrolls for ``node``: a repeat multiplies by its largest finite count, the rest add."""
    for kind, refusal in _REFUSED_NODES.items():
        if isinstance(node, kind):
            raise UserRegexError(f"{what}: {refusal}")
    if isinstance(node, _REPEATS):
        factor = node.max_count if node.max_count is not None else node.min_count
        return max(factor, 1) * _expansion(node.subpattern, what)
    if isinstance(node, _SEQUENCE):
        return sum(_expansion(item, what) for item in node.items) or 1
    if isinstance(node, _BRANCH):
        return sum(_expansion(branch, what) for branch in node.branches) or 1
    if isinstance(node, _PASS_THROUGH):
        return _expansion(node.subpattern, what)
    if isinstance(node, _LOOKAROUND_CONDITIONAL):
        return _expansion(node.subpattern, what) + _expansion(node.yes_item, what) + _expansion(node.no_item, what)
    if isinstance(node, _CONDITIONAL):
        return _expansion(node.yes_item, what) + _expansion(node.no_item, what)
    if isinstance(node, _STRING):
        return max(len(node.characters), 1)
    if isinstance(node, _core.Grapheme):
        return _GRAPHEME_WEIGHT
    if isinstance(node, _core.SetBase):
        return _set_weight(node)
    if isinstance(node, _LEAVES):
        return 1
    raise UserRegexError(f"{what} uses a construct the bounds do not know ({type(node).__name__})")


def _set_weight(node: Any) -> int:
    """A character set: 1, plus 1 per property in it, plus 1 per 16 other members (measured: each property is a table)."""
    properties = others = 0
    stack = list(node.items)
    while stack:
        item = stack.pop()
        if isinstance(item, _core.SetBase):
            stack.extend(item.items)
        elif isinstance(item, _core.Property):
            properties += 1
        else:
            others += 1
    return 1 + properties + others // 16


def repeat_expansion(pattern: str, flags: int = 0, *, what: str = "pattern") -> int:
    """How far the engine unrolls ``pattern``, read from the engine's own parse tree (no length or cap check).

    Raises :class:`UserRegexError` for a pattern the engine cannot parse or a
    construct the tree walk refuses (fuzzy matching, group calls).
    """
    try:
        tree = _parse(pattern, flags)
    except (regex.error, RecursionError, MemoryError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise UserRegexError(f"invalid regex: {exc}") from None
    try:
        return _expansion(tree, what)
    except RecursionError:
        raise UserRegexError(f"{what} nests too deeply") from None


def check_user_pattern(
    pattern: str, flags: int = 0, *, max_length: int | None = MAX_PATTERN_LENGTH, what: str = "pattern",
) -> int:
    """Refuse a pattern that is too long, uses a refused construct, or unrolls too far; return its expansion.

    Nothing is compiled: the lexical refusals run first, then the engine's
    parser builds the tree and the walk prices it. Raises :class:`UserRegexError`.
    """
    if max_length is not None and len(pattern) > max_length:
        raise UserRegexError(f"{what} is {len(pattern)} characters long; the limit is {max_length}")
    _refuse_constructs(pattern, what)
    expansion = repeat_expansion(pattern, flags, what=what)
    if expansion > MAX_REPEAT_EXPANSION:
        raise UserRegexError(
            f"{what} repeats too much to compile (its counted repeats unroll to {expansion} steps; "
            f"the limit is {MAX_REPEAT_EXPANSION}); use smaller {{n}} counts or * and +"
        )
    return expansion


class _TransientRefusal(UserRegexError):
    """A refusal that depends on the process's state (memory, stack depth), so it is never cached."""


def _compile_checked(pattern: str, flags: int, what: str) -> regex.Pattern:
    try:
        return regex.compile(pattern, flags | regex.VERSION0, cache_pattern=False)
    except (MemoryError, RecursionError):
        raise _TransientRefusal(
            f"{what} repeats too much to compile; use smaller {{n}} counts or * and +"
        ) from None
    except OverflowError:
        raise UserRegexError(
            f"{what} repeats too much to compile; use smaller {{n}} counts or * and +"
        ) from None
    except (regex.error, ValueError) as exc:
        raise UserRegexError(f"invalid regex: {exc}") from None


# --------------------------------------------------------------------------- the stored-pattern cache


class _StoredCache:
    """Compiled stored patterns and the check's verdicts, LRU, bounded by total expansion and entry count."""

    def __init__(self, budget: int, max_entries: int) -> None:
        self._budget = budget
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple, tuple[regex.Pattern | None, str | None, int]] = OrderedDict()
        self._weight = 0
        self._lock = threading.Lock()

    def get(self, key: tuple) -> tuple[regex.Pattern | None, str | None] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[0], entry[1]

    def put(self, key: tuple, compiled: regex.Pattern | None, refusal: str | None, weight: int) -> None:
        if weight > self._budget:
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._weight -= old[2]
            self._entries[key] = (compiled, refusal, weight)
            self._weight += weight
            while self._weight > self._budget or len(self._entries) > self._max_entries:
                _key, (_c, _r, dropped) = self._entries.popitem(last=False)
                self._weight -= dropped

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def weight(self) -> int:
        return self._weight

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._weight = 0


_STORED = _StoredCache(_CACHE_EXPANSION_BUDGET, _CACHE_MAX_ENTRIES)


def stored_cache_size() -> tuple[int, int]:
    """(entries, total expansion) the stored-pattern cache holds."""
    return len(_STORED), _STORED.weight


def compile_user_regex(
    pattern: str,
    flags: int = 0,
    *,
    max_length: int | None = MAX_PATTERN_LENGTH,
    what: str = "pattern",
    stored: bool = False,
) -> regex.Pattern:
    """Check and compile a user pattern with the ``regex`` engine.

    ``flags`` are ``regex`` flags (``IGNORECASE``, ``DOTALL``, ``MULTILINE``).
    ``stored=True`` is for the MATCH path of a pattern the platform keeps
    and runs again (a subscription, a filter, a schema): its compile AND its
    verdict (a refusal too) are cached, so a hit neither re-checks nor
    recompiles. A save-time check and a one-shot pattern pass
    ``stored=False`` and never touch the cache, and a refusal that depends on
    the process's state (``MemoryError``, ``RecursionError``) is never cached.
    Raises :class:`UserRegexError`, whose message starts with
    ``invalid regex`` when the engine refuses the syntax.
    """
    key = (pattern, flags, max_length, what)
    if stored:
        hit = _STORED.get(key)
        if hit is not None:
            compiled, refusal = hit
            if refusal is not None:
                raise UserRegexError(refusal)
            return compiled
    try:
        expansion = check_user_pattern(pattern, flags, max_length=max_length, what=what)
        compiled = _compile_checked(pattern, flags, what)
    except UserRegexError as exc:
        if stored and not isinstance(exc, _TransientRefusal):
            _STORED.put(key, None, str(exc), 1)
        raise
    if stored:
        _STORED.put(key, compiled, None, expansion + 1)
    return compiled


def can_match_newline(node: Any) -> bool:
    """Whether a leaf of the parse tree could consume ``\\n`` (refused by the line scanner).

    ``.`` under DOTALL; a negated set; a character, range or property that
    matches ``\\n`` (``matches(10)``: ``\\n``, ``\\s``, ``\\W``, ``[^y]``, ``[\\x00-z]``); a
    string holding ``\\n``; a positive set with any such member.
    """
    if isinstance(node, _core.AnyAll):
        return True
    if isinstance(node, _core.SetBase):
        if not node.positive:
            return True
        return any(can_match_newline(item) for item in node.items)
    if isinstance(node, (_core.Character, _core.Range, _core.Property)):
        return bool(node.matches(10))
    if isinstance(node, _STRING):
        return 10 in node.characters
    return False


# One group of global inline flags at the very start (the only place the check allows one).
_LEADING_FLAGS = regex.compile(r"\(\?[aimsu]+\)")


def line_scanner(pattern: str, flags: int = 0, *, what: str = "pattern") -> regex.Pattern | None:
    """One pattern that finds, in ONE timed scan of a file, every line on which ``pattern`` matches by itself; or ``None``.

    The lines are joined by ``\\n`` and scanned with ``(?m)(?:pattern)[^\\n]*``:
    each match starts where ``pattern`` first matches in a line and runs on
    to the line's end, so the scan resumes at the next line, a line yields at
    most one match (plus, at most, an empty one at its end), and ONE
    ``finditer`` over the text lists exactly the matching lines: one timed
    call per file, since the engine's ``timeout=`` alone costs about 1.4 us a
    call, and with ``pattern`` first the engine keeps its fast skip to
    ``pattern``'s literal text.

    That holds only when nothing in ``pattern`` can see or cross a line end
    (#859 round 3, R2-2: a leaf that can consume ``\\n``, such as ``[^y]``,
    made round 2's fast path quadratic). So the scanner is offered only for a
    backtracking-complete pattern built from literals, sets, ``.`` without
    DOTALL, groups, alternatives, greedy and lazy repeats, line anchors and
    word boundaries, with NO leaf that can match ``\\n``
    (:func:`can_match_newline`). Anything else (lookaround, ``\\A``/``\\Z``,
    atomic and possessive groups, backreferences, conditionals, ``\\G``,
    backtracking verbs, ``\\X``) gets ``None`` and the caller searches line by
    line.
    """
    try:
        tree = _parse(pattern, flags | regex.MULTILINE)
    except Exception:  # noqa: BLE001 - no scanner is always correct
        return None
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, _POSSESSIVE):
            return None
        if isinstance(node, _REPEATS):
            stack.append(node.subpattern)
        elif isinstance(node, _SEQUENCE):
            stack.extend(node.items)
        elif isinstance(node, _BRANCH) and not isinstance(node, _core.StringSet):
            stack.extend(node.branches)
        elif isinstance(node, _core.Group):
            stack.append(node.subpattern)
        elif isinstance(node, _core.ZeroWidthBase):
            if not isinstance(node, _LINE_SAFE_ZERO_WIDTH):
                return None
        elif isinstance(node, (_core.Character, _core.SetBase, _core.Any, _core.Property, _core.Range, _STRING)):
            if can_match_newline(node):
                return None
        else:
            return None
    leading = _LEADING_FLAGS.match(pattern)
    head, body = (leading.group(0), pattern[leading.end():]) if leading else ("", pattern)
    return _compile_checked(f"{head}(?:{body})[^\\n]*", flags | regex.MULTILINE, what)


# --------------------------------------------------------------------------- time


def timed(call: Callable[[float], T], *, timeout: float, what: str = "pattern", cause: str = "budget") -> T:
    """Run ``call(timeout)`` (a ``regex`` method given ``timeout=``) and turn the engine's timeout into :class:`UserRegexTimeout`."""
    if timeout <= 0:
        raise UserRegexTimeout(f"{what} matching ran out of time", seconds=0.0, cause=cause)
    try:
        return call(timeout)
    except TimeoutError:
        raise UserRegexTimeout(
            f"{what} matching ran past its {timeout:g} s bound", seconds=timeout, cause=cause,
        ) from None


def timed_search(compiled: regex.Pattern, text: str, *, timeout: float) -> regex.Match | None:
    """``compiled.search(text)`` bounded by ``timeout`` seconds, with the GIL released."""
    return timed(lambda t: compiled.search(text, timeout=t, concurrent=True), timeout=timeout, cause="per_search")


class MatchBudget:
    """One total time bound shared by many matches, optionally drawing on a ``parent`` budget too.

    The budget counts only the time charged to it (engine time, and compile
    time where a caller charges it), so file reads and walks around a grep do
    not use it up. Each search runs with a timeout of what is left here and
    in the parent (or ``per_search``, when smaller) and raises
    :class:`UserRegexTimeout` when the engine stops it or nothing is left;
    the exception's ``cause`` and :attr:`exhausted` say which bound ended
    it. :attr:`searches` and :attr:`longest` record the work done. Not
    thread-safe: use one budget from one thread at a time.
    """

    def __init__(
        self, seconds: float, *, per_search: float | None = None, what: str = "pattern",
        parent: MatchBudget | None = None,
    ) -> None:
        self.seconds = seconds
        self.per_search = per_search
        self.what = what
        self.parent = parent
        self.spent = 0.0
        self.searches = 0
        self.longest = 0.0

    @property
    def remaining(self) -> float:
        own = max(self.seconds - self.spent, 0.0)
        return own if self.parent is None else min(own, self.parent.remaining)

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.seconds or (self.parent is not None and self.parent.exhausted)

    def charge(self, seconds: float) -> None:
        """Count ``seconds`` of work (a compile, say) against this budget and its parent.

        The parent is charged at most what was left of this budget, so work
        that overshoots one share (a slow compile) cannot spend the time of
        the parent's other shares (#859 round 3, R2-1).
        """
        left = max(self.seconds - self.spent, 0.0)
        self.spent = min(self.spent + seconds, self.seconds)
        if self.parent is not None:
            self.parent.charge(min(seconds, left))

    def _exhaust(self) -> None:
        """Mark the budget in the chain that bound the last search as spent."""
        own = self.seconds - self.spent
        if self.parent is None or own <= self.parent.remaining:
            self.spent = self.seconds
        else:
            self.parent._exhaust()

    def _run(self, call: Callable[[float], T], *, per_search: bool = True) -> T:
        remaining = self.remaining
        if remaining <= 0:
            raise UserRegexTimeout(
                f"{self.what} matching used its {self.seconds:g} s budget", seconds=self.seconds,
            )
        bound = remaining if (self.per_search is None or not per_search) else min(remaining, self.per_search)
        started = time.perf_counter()
        try:
            result = timed(call, timeout=bound, what=self.what, cause="per_search")
        except UserRegexTimeout:
            self._account(time.perf_counter() - started)
            if bound >= remaining:
                # The whole budget (this one, or the parent's) ran out.
                self._exhaust()
                raise UserRegexTimeout(
                    f"{self.what} matching used its {self.seconds:g} s budget", seconds=self.seconds,
                ) from None
            raise
        self._account(time.perf_counter() - started)
        return result

    def _account(self, elapsed: float) -> None:
        self.searches += 1
        self.longest = max(self.longest, elapsed)
        self.charge(elapsed)

    def search(self, compiled: regex.Pattern, text: str) -> regex.Match | None:
        return self._run(lambda t: compiled.search(text, timeout=t, concurrent=True))

    def whole(self, call: Callable[[float], T]) -> T:
        """Run ``call(timeout)``, one whole-text pass over a file, bounded by the budget alone (not ``per_search``)."""
        return self._run(call, per_search=False)

    def match(self, compiled: regex.Pattern, text: str) -> regex.Match | None:
        return self._run(lambda t: compiled.match(text, timeout=t, concurrent=True))

    def findall_matches(self, compiled: regex.Pattern, text: str) -> list[regex.Match]:
        """Every match of ``compiled`` in ``text`` (``finditer``, materialised under ONE timeout).

        A whole-text scan: bounded by the budget alone, not ``per_search``.
        """
        return self._run(lambda t: list(compiled.finditer(text, timeout=t, concurrent=True)), per_search=False)


def matching_lines(
    compiled: regex.Pattern,
    lines: list[str],
    budget: MatchBudget,
    *,
    scanner: regex.Pattern | None = None,
    limit: int | None = None,
) -> list[int]:
    """The indexes of the ``lines`` that ``compiled`` matches (a ``search`` of each line on its own), in order.

    With a :func:`line_scanner` the lines are joined by ``\\n`` and the answer
    is ONE timed ``finditer`` of the scanner over the text, bounded by the
    budget alone: each scanner match starts at the start of a matching line.
    Without one, each line is searched on its own, under ``per_search``.

    At most ``limit`` indexes. A :class:`UserRegexTimeout` carries ``line``:
    the index being searched, or for a scan the last line found so far.
    """
    out: list[int] = []
    if not lines:
        return out

    if scanner is None:
        for index in range(len(lines)):
            try:
                found = budget.search(compiled, lines[index])
            except UserRegexTimeout as exc:
                exc.line = index
                raise
            if found is not None:
                out.append(index)
                if limit is not None and len(out) >= limit:
                    break
        return out

    joined = "\n".join(lines)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line) + 1

    def _scan(timeout: float) -> list[int]:
        for found in scanner.finditer(joined, timeout=timeout, concurrent=True):
            index = bisect.bisect_right(starts, found.start()) - 1
            if out and out[-1] == index:
                # After an empty match at a line start the engine may match again from
                # the same start (a non-empty match): the same line, already counted.
                continue
            out.append(index)
            if limit is not None and len(out) >= limit:
                break
        return out

    try:
        return budget.whole(_scan)
    except UserRegexTimeout as exc:
        exc.line = out[-1] if out else 0
        raise


# --------------------------------------------------------------------------- the executor

# Bounded regex work that must leave the event loop runs on one of two small
# executors, never the default to_thread pool. Bulk work (grep over files,
# collection grep) cannot delay interactive work (a tap read's LIKE, a graph
# node's output_schema): four hostile greps hold the bulk threads, not these.
_INTERACTIVE = ThreadPoolExecutor(max_workers=4, thread_name_prefix="user-regex-interactive")
_BULK = ThreadPoolExecutor(max_workers=4, thread_name_prefix="user-regex-bulk")


async def _run_on(executor: ThreadPoolExecutor, fn: Callable[..., T], args: tuple, kwargs: dict) -> T:
    context = contextvars.copy_context()
    call = functools.partial(context.run, fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(executor, call)


async def run_interactive(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``fn(*args, **kwargs)`` on the interactive user-regex executor (tap LIKE, output_schema), with the caller's context."""
    return await _run_on(_INTERACTIVE, fn, args, kwargs)


async def run_bulk(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``fn(*args, **kwargs)`` on the bulk user-regex executor (grep, collection grep), with the caller's context."""
    return await _run_on(_BULK, fn, args, kwargs)


def _self_check() -> None:
    """Prove at import that the walker reads this regex release's tree as it expects."""
    for pattern, expected in (("(?:a{3}){4}", 12), ("a|bc", 3), ("x{2,5}", 5), ("(?:ab)*c", 3)):
        got = check_user_pattern(pattern)
        if got != expected:  # pragma: no cover - exercised only by a regex upgrade
            raise RuntimeError(
                f"primer.common.user_regex reads {pattern!r} as {got} steps, not {expected}; "
                "the regex parser changed and the bounds need review"
            )


_self_check()


__all__ = [
    "MAX_PATTERN_LENGTH",
    "MAX_REPEAT_EXPANSION",
    "MatchBudget",
    "UserRegexError",
    "UserRegexTimeout",
    "check_user_pattern",
    "compile_user_regex",
    "can_match_newline",
    "line_scanner",
    "matching_lines",
    "repeat_expansion",
    "run_bulk",
    "run_interactive",
    "stored_cache_size",
    "timed",
    "timed_search",
]
