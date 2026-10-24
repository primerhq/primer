"""primer.common.user_regex: the size, compile, memory and time bounds every user regex runs under (G6-4, G6-5, G6-11).

Round 2 of #859: the size check reads the ``regex`` engine's OWN parse tree (round 1's hand scanner was fooled by comment
groups, verbose mode and inline flags), the constructs a search box never needs are refused before parsing, the stored-pattern
cache is bounded by the expansion it holds, and one-shot patterns never enter it. Nothing in this module compiles a blow-up
pattern: the checks parse without compiling, and tests/common/test_user_regex_entry_points.py drives the real entry points
in child processes.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time

import pytest
import regex

import primer.common.user_regex as user_regex
from primer.common.user_regex import (
    MAX_PATTERN_LENGTH,
    MAX_REPEAT_EXPANSION,
    MatchBudget,
    UserRegexError,
    UserRegexTimeout,
    check_user_pattern,
    compile_user_regex,
    can_match_newline,
    line_scanner,
    matching_lines,
    repeat_expansion,
    run_bulk,
    run_interactive,
    stored_cache_size,
    timed_search,
)

# Exponential in both engines and not caught by any shape check: about 90 s
# for the regex engine on this text when nothing bounds it.
HOSTILE = r"(a|aa)+$"
HOSTILE_TEXT = "a" * 60 + "!"

# The review's bypasses of round 1's scanner, with the expansion the ENGINE's tree gives each.
REVIEW_SHAPES = {
    "(?#[)(?:a{65535}){65535}]": 65535 * 65535,
    "(?:(?:a{300})(?#){300})(?#){300}": 300 ** 3,
    "(?x)(?:(?:a{300}) {300}) {300}": 300 ** 3,
    "(?:(?:a{300})(?i){300})(?i){300}": 300 ** 3,
    "(?x)#[\n(?:a{1000}){1000}]": 1000 * 1000,
    "(?x)(?:a{10 00}){10 00}": 1000 * 1000,
}


def test_the_caps():
    assert MAX_PATTERN_LENGTH == 1000
    assert MAX_REPEAT_EXPANSION == 10_000


# ---- B1: the engine's parse tree, and what is refused before it --------------------------------------------------------


@pytest.mark.parametrize("pattern", sorted(REVIEW_SHAPES))
def test_the_walker_reads_each_review_shape_as_the_engine_does(pattern):
    # repeat_expansion parses with the engine's parser and walks the tree; it never compiles.
    assert repeat_expansion(pattern) >= REVIEW_SHAPES[pattern] > MAX_REPEAT_EXPANSION


@pytest.mark.parametrize("pattern", sorted(REVIEW_SHAPES) + ["(?i:[)(?:a{65535}){65535}]"])
def test_the_check_refuses_each_review_shape_without_compiling(pattern, monkeypatch):
    def _no_compile(*args, **kwargs):
        raise AssertionError("the check compiled a pattern")

    monkeypatch.setattr(user_regex.regex, "compile", _no_compile)
    started = time.perf_counter()
    with pytest.raises(UserRegexError):
        check_user_pattern(pattern)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "pattern, refusal",
    [
        ("ab(?#comment)c", "comment groups"),
        ("(?x)a b", "verbose mode"),
        ("(?ix)ab", "verbose mode"),
        ("(?x:a b)", "verbose mode"),
        ("a(?i)b", "only in one (?aimsu) group at the very start"),
        ("(?i:a)b", "only in one (?aimsu) group at the very start"),
        ("(?V1)a", "only in one (?aimsu) group at the very start"),
        ("(?R)", "group calls or recursion"),
        ("(a)(?1)", "group calls or recursion"),
        ("(?P<x>a)(?P>x)", "group calls or recursion"),
        ("(?<x>a)(?&x)", "group calls or recursion"),
        ("(?:foo){e<=1}", "fuzzy matching"),
        ("(?:foo){i<=1,d<=1}", "fuzzy matching"),
        ("(?:foo){s<=2}", "fuzzy matching"),
        ("(?:foo){e<=1:[a-z]}", "fuzzy matching"),
        ("(?:foo){1<=e<=2}", "fuzzy matching"),
        ("(?:a{100}){101}", "repeats too much"),
        ("a{10001}", "repeats too much"),
        ("(unclosed", "invalid regex"),
        ("a**", "invalid regex"),
        ("(?'name'x)", "(?'name'...) named groups"),
        ("(?*x)", "a group construct that is not supported: (?*"),
        ("(" * 499 + ")" * 499, "invalid regex"),
    ],
)
def test_constructs_a_search_box_never_needs_are_refused(pattern, refusal):
    with pytest.raises(UserRegexError, match=regex.escape(refusal)):
        check_user_pattern(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"^\d{4}-\d{2}-\d{2}$",
        r"(?:ab{1,30}){1,30}",
        r"\p{Lu}\{1000000\}",
        r"[{1000000}]x",
        r"[[:alpha:]]{3}",
        r"\N{LATIN SMALL LETTER A}{3}",
        "(?:a{100}){100}",
        "(?i)deploy",
        "(?ms)^a.b$",
        r"(?P<word>\w+) (?P=word)",
        r"(?<=a)b(?!c)",
        r"(?>ab|a)c",
        r"(a)(?(1)b|c)",
        r"(?|(a)|(b))",
        "TODO|FIXME",
    ],
)
def test_ordinary_patterns_pass(pattern):
    assert check_user_pattern(pattern) <= MAX_REPEAT_EXPANSION
    compile_user_regex(pattern)


def test_the_length_cap():
    compile_user_regex("a" * MAX_PATTERN_LENGTH)
    with pytest.raises(UserRegexError, match="1001 characters long; the limit is 1000"):
        compile_user_regex("a" * 1001)
    compile_user_regex("a" * 1001, max_length=None)


def test_the_engine_api_the_walker_reads_is_pinned():
    from regex import _regex_core

    for name in ("Source", "Info", "_parse_pattern", "_UnscopedFlagSet", "GreedyRepeat", "LazyRepeat",
                 "PossessiveRepeat", "Sequence", "Branch", "Group", "Atomic", "LookAround", "Conditional",
                 "LookAroundConditional", "Fuzzy", "CallGroup", "CallRef", "StringSet", "String", "Character",
                 "SetBase", "Any", "Property", "Range", "RefGroup", "Grapheme", "ZeroWidthBase"):
        assert hasattr(_regex_core, name), name
    assert repeat_expansion("(?:a{3}){4}") == 12
    assert repeat_expansion("a|bc") == 3


@pytest.mark.parametrize("error", [MemoryError, RecursionError, OverflowError])
def test_a_compile_that_runs_out_of_memory_is_a_refusal(monkeypatch, error):
    def _raise(*args, **kwargs):
        raise error()

    monkeypatch.setattr(user_regex.regex, "compile", _raise)
    with pytest.raises(UserRegexError, match="repeats too much to compile"):
        compile_user_regex("plain-pattern-for-the-memory-test")


@pytest.mark.parametrize("error", [MemoryError, RecursionError])
def test_a_refusal_that_depends_on_the_process_is_never_cached(monkeypatch, error):
    """R2-8: a MemoryError at one moment must not refuse the stored pattern forever."""
    pattern = f"transient-{error.__name__}-[0-9]+"
    real = user_regex.regex.compile

    def _raise(*args, **kwargs):
        raise error()

    monkeypatch.setattr(user_regex.regex, "compile", _raise)
    with pytest.raises(UserRegexError):
        compile_user_regex(pattern, stored=True)
    monkeypatch.setattr(user_regex.regex, "compile", real)
    assert compile_user_regex(pattern, stored=True).search(f"transient-{error.__name__}-12")


def test_costly_leaves_weigh_more():
    """R2-4: \\X compiles to about 4.4x the memory and 7x the time of a plain character; a set's properties are tables."""
    assert repeat_expansion(r"\X") == 8
    assert check_user_pattern(r"(?:\X{100}){12}") == 9600
    with pytest.raises(UserRegexError, match="repeats too much"):
        check_user_pattern(r"(?:\X{100}){13}")
    assert repeat_expansion(r"[\p{L}\p{N}]") == 3
    assert repeat_expansion("[abcdefghijklmnopqrstuvwxyz0123456789]") == 3
    assert repeat_expansion("[a-z]") == 1


# ---- B2: memory through the cache ---------------------------------------------------------------------------------------


def test_one_shot_patterns_never_enter_a_cache():
    before = stored_cache_size()
    engine_before = len(regex._main._cache)
    for index in range(100):
        compile_user_regex(f"(?:a{{99}}){{100}}one-shot-{index:03d}")
    assert stored_cache_size() == before
    assert len(regex._main._cache) == engine_before


def test_stored_patterns_are_cached_with_their_verdict(monkeypatch):
    calls: list[str] = []
    real = user_regex.check_user_pattern

    def _counting(pattern, *args, **kwargs):
        calls.append(pattern)
        return real(pattern, *args, **kwargs)

    monkeypatch.setattr(user_regex, "check_user_pattern", _counting)
    first = compile_user_regex("stored-and-cached-[0-9]+", stored=True)
    assert compile_user_regex("stored-and-cached-[0-9]+", stored=True) is first
    for _ in range(2):
        with pytest.raises(UserRegexError, match="repeats too much"):
            compile_user_regex("(?:b{101}){100}stored-refusal", stored=True)
    assert calls == ["stored-and-cached-[0-9]+", "(?:b{101}){100}stored-refusal"]


def test_the_stored_cache_is_bounded_by_the_expansion_it_holds():
    for index in range(40):
        compile_user_regex(f"(?:c{{99}}){{100}}near-cap-{index:03d}", stored=True)
    entries, weight = stored_cache_size()
    assert weight <= user_regex._CACHE_EXPANSION_BUDGET
    assert entries <= user_regex._CACHE_MAX_ENTRIES


# ---- time -------------------------------------------------------------------------------------------------------------


def test_a_budget_bounds_a_catastrophic_search():
    budget = MatchBudget(0.05)
    started = time.perf_counter()
    with pytest.raises(UserRegexTimeout) as stopped:
        budget.search(regex.compile(HOSTILE), HOSTILE_TEXT)
    assert time.perf_counter() - started < 0.5
    assert budget.exhausted and stopped.value.cause == "budget"
    with pytest.raises(UserRegexTimeout):
        budget.search(regex.compile("a"), "a")


def test_a_per_search_bound_does_not_spend_the_whole_budget():
    budget = MatchBudget(5.0, per_search=0.01)
    with pytest.raises(UserRegexTimeout) as stopped:
        budget.search(regex.compile(HOSTILE), HOSTILE_TEXT)
    assert stopped.value.cause == "per_search"
    assert not budget.exhausted
    assert 0.005 < budget.spent < 1.0
    assert budget.search(regex.compile("b+"), "abbb").group(0) == "bbb"
    assert budget.searches == 2 and budget.longest >= 0.005


def test_a_child_budget_draws_on_its_parent():
    parent = MatchBudget(0.05)
    first, second = MatchBudget(1.0, parent=parent), MatchBudget(1.0, parent=parent)
    with pytest.raises(UserRegexTimeout):
        first.search(regex.compile(HOSTILE), HOSTILE_TEXT)
    assert parent.exhausted and second.exhausted
    child = MatchBudget(0.02, parent=MatchBudget(5.0))
    child.charge(0.03)
    assert child.exhausted and not child.parent.exhausted


def test_finditer_runs_under_one_bound_for_the_whole_iteration():
    budget = MatchBudget(0.05)
    with pytest.raises(UserRegexTimeout):
        budget.findall_matches(regex.compile("x"), "x" * 5_000_000)
    assert MatchBudget(1.0).findall_matches(regex.compile("b"), "abcb")[1].start() == 3


def test_timed_search_answers_and_times_out():
    assert timed_search(regex.compile("b"), "abc", timeout=1.0).start() == 1
    with pytest.raises(UserRegexTimeout):
        timed_search(regex.compile(HOSTILE), HOSTILE_TEXT, timeout=0.02)


async def test_bounded_work_runs_on_the_dedicated_executor_and_leaves_the_loop_running():
    """L1: not the default to_thread pool. concurrent=True releases the GIL, so the loop keeps ticking."""
    gaps: list[float] = []
    state = {"stop": False}

    async def ticker() -> None:
        last = time.perf_counter()
        while not state["stop"]:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.03)
    budget = MatchBudget(0.4)

    def _work() -> str:
        try:
            budget.search(regex.compile(HOSTILE), HOSTILE_TEXT)
        except UserRegexTimeout:
            pass
        return threading.current_thread().name

    name = await run_interactive(_work)
    state["stop"] = True
    await task
    assert name.startswith("user-regex-interactive")
    assert max(gaps) < 0.2, max(gaps)


async def test_hostile_bulk_work_does_not_delay_interactive_work():
    """R2-3: four greps that run their whole budget hold the bulk threads, not the interactive ones."""
    hostile = regex.compile(HOSTILE)

    def _slow() -> str:
        try:
            MatchBudget(1.0).search(hostile, HOSTILE_TEXT)
        except UserRegexTimeout:
            pass
        return threading.current_thread().name

    def _tiny() -> str:
        MatchBudget(1.0).search(regex.compile("x"), "abc x")
        return threading.current_thread().name

    jobs = [asyncio.create_task(run_bulk(_slow)) for _ in range(4)]
    await asyncio.sleep(0.05)
    started = time.perf_counter()
    name = await run_interactive(_tiny)
    waited = time.perf_counter() - started
    names = await asyncio.gather(*jobs)
    assert waited < 0.2, waited
    assert name.startswith("user-regex-interactive")
    assert all(n.startswith("user-regex-bulk") for n in names), names


def test_a_share_charges_its_parent_at_most_what_it_had_left():
    """R2-1 (b): a compile that overshoots one owner's share cannot spend the other owners' time."""
    pool = MatchBudget(1.0)
    share = MatchBudget(0.02, parent=pool)
    share.charge(0.5)
    assert share.exhausted
    assert pool.spent == pytest.approx(0.02)
    share.charge(0.5)
    assert pool.spent == pytest.approx(0.02)


# ---- the line scanner: exact, and only where nothing can cross a line end (round 3, R2-2) -------------------------------


@pytest.mark.parametrize(
    "pattern",
    [r"(?<=a)b", r"\Afoo", r"foo\Z", r"(?>a+)b", r"a++b", r"(a)\1", r"a(?=b)", r"\Gx", r"(?(1)a|b)(a)", r"a\Kb", r"\X"],
)
def test_constructs_that_see_past_a_line_get_no_scanner(pattern):
    assert line_scanner(pattern) is None


@pytest.mark.parametrize(
    "pattern, leaf",
    [
        (r"(?s)a.b", "AnyAll: . under DOTALL"),
        (r"x[^y]*z", "a negated character, the review's quadratic case"),
        (r"x[^ab]*z", "a negated set"),
        (r"a\nb", "a character that is \n"),
        (r"[\x00-z]+", "a range holding \n"),
        (r"a\s+b", "a property holding \n"),
        (r"a\W+b", "a negated property holding \n"),
        (r"[a\s]+", "a positive set with a property holding \n"),
        (r"[a\n]+", "a positive set with \n"),
        (r"[[:space:]]", "a POSIX class holding \n"),
    ],
)
def test_a_leaf_that_can_consume_a_newline_gets_no_scanner(pattern, leaf):
    assert line_scanner(pattern) is None, leaf


def test_a_string_holding_a_newline_can_consume_one():
    from regex import _regex_core

    assert can_match_newline(_regex_core.String([ord("a"), 10]))
    assert not can_match_newline(_regex_core.String([ord("a"), ord("b")]))


@pytest.mark.parametrize("pattern", [r"[^\n]x", r"\S+=", r"a.b", r"[a-z]+\d", r"(?i)^def \w+$", r"\bTODO\b|FIXME"])
def test_line_local_patterns_get_a_scanner(pattern):
    assert line_scanner(pattern) is not None


def test_the_reviews_quadratic_case_is_linear():
    """R2-2: x[^y]*z over 8,001 lines ran past the 10 s budget on round 2's fast path."""
    lines = ["x" * 30] * 8000 + ["yz"]
    compiled = compile_user_regex(r"x[^y]*z")
    started = time.perf_counter()
    got = matching_lines(compiled, lines, MatchBudget(10.0), scanner=line_scanner(r"x[^y]*z"))
    assert got == []
    assert time.perf_counter() - started < 1.0


_ATOMS = ["a", "b", ".", r"\s", r"\S", r"\w", r"\W", "[ab]", "[^a]", r"[^\n]", r"\d", r"\n", "x", " "]
_EDGES = ["^", "$", r"\b", r"\B"]


def _random_pattern(rng: random.Random, depth: int = 0) -> str:
    parts = []
    for _ in range(rng.randint(1, 4)):
        roll = rng.random()
        if roll < 0.15 and depth < 2:
            inner = "|".join(_random_pattern(rng, depth + 1) for _ in range(rng.randint(1, 3)))
            part = f"(?:{inner})"
        elif roll < 0.25:
            part = rng.choice(_EDGES)
        else:
            part = rng.choice(_ATOMS)
        if rng.random() < 0.35 and part not in _EDGES:
            part += rng.choice(["*", "+", "?", "{0,2}", "{1,3}", "*?", "+?"])
        parts.append(part)
    return "".join(parts)


# Every separator str.splitlines() splits on, so the scanner sees them only as line ends.
_ALPHABET = [
    "a", "b", "x", " ", "1", "\t", "ab", "aa",
    "\n", "\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ",
]


def _random_text(rng: random.Random) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 30)))


def test_the_scanned_line_search_finds_exactly_the_lines_a_per_line_search_finds():
    rng = random.Random(859)
    checked = scanned = 0
    for _ in range(4000):
        pattern = _random_pattern(rng)
        flags = regex.IGNORECASE if rng.random() < 0.3 else 0
        if rng.random() < 0.2:
            pattern = "(?i)" + pattern
        try:
            compiled = compile_user_regex(pattern, flags)
        except UserRegexError:
            continue
        scanner = line_scanner(pattern, flags)
        for _ in range(3):
            lines = _random_text(rng).splitlines()
            expected = [i for i, line in enumerate(lines) if compiled.search(line)]
            got = matching_lines(compiled, lines, MatchBudget(5.0), scanner=scanner)
            assert got == expected, (pattern, flags, lines)
            checked += 1
            scanned += scanner is not None
    assert checked > 3000 and scanned > 1000, (checked, scanned)
