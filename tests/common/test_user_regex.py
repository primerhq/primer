"""primer.common.user_regex: the size, compile and time bounds every user regex runs under (G6-4, G6-5, G6-11)."""

from __future__ import annotations

import asyncio
import time

import pytest
import regex

from primer.common.user_regex import (
    MAX_PATTERN_LENGTH,
    MAX_REPEAT_EXPANSION,
    MatchBudget,
    UserRegexError,
    UserRegexTimeout,
    compile_user_regex,
    repeat_expansion,
    timed_search,
)

# Exponential in both engines and not caught by any shape check: about 90 s
# for the regex engine on this text when nothing bounds it.
HOSTILE = r"(a|aa)+$"
HOSTILE_TEXT = "a" * 60 + "!"


def test_a_pattern_over_the_length_cap_is_refused_before_compiling():
    compile_user_regex("a" * MAX_PATTERN_LENGTH)
    with pytest.raises(UserRegexError, match=f"1001 characters long; the limit is {MAX_PATTERN_LENGTH}"):
        compile_user_regex("a" * 1001)
    # A caller with its own (or no) length cap passes it.
    compile_user_regex("a" * 1001, max_length=None)


@pytest.mark.parametrize(
    "pattern",
    ["(?:a{65535}){65535}", "(?:a{1000}){1000}", "(?:(?:(?:a{50}){50}){50})", "a{100001}", "a{999}" * 101],
)
def test_counted_repeats_that_unroll_past_the_cap_are_refused_before_compiling(pattern):
    started = time.perf_counter()
    with pytest.raises(UserRegexError, match="repeats too much"):
        compile_user_regex(pattern, max_length=None)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "pattern",
    [
        r"^\d{4}-\d{2}-\d{2}$",
        r"(?:ab{1,30}){1,30}",
        r"(?:deploy){e<=2}",
        r"\p{Lu}\{1000000\}",
        r"[{1000000}]x",
        r"\N{LATIN SMALL LETTER A}{3}",
        "(?:a{300}){300}",
    ],
)
def test_ordinary_patterns_are_within_the_expansion_cap(pattern):
    assert repeat_expansion(pattern) <= MAX_REPEAT_EXPANSION
    compile_user_regex(pattern)


@pytest.mark.parametrize("pattern", ["(unclosed", "a**", "(" * 499 + ")" * 499])
def test_a_pattern_that_does_not_compile_is_a_user_regex_error(pattern):
    with pytest.raises(UserRegexError, match="invalid regex"):
        compile_user_regex(pattern)


def test_compiled_patterns_are_cached_and_use_the_regex_engine():
    first = compile_user_regex(r"^cached-[a-z]+$", regex.IGNORECASE)
    assert compile_user_regex(r"^cached-[a-z]+$", regex.IGNORECASE) is first
    assert isinstance(first, regex.Pattern)
    assert first.search("CACHED-abc")


def test_a_budget_bounds_a_catastrophic_search():
    budget = MatchBudget(0.05)
    started = time.perf_counter()
    with pytest.raises(UserRegexTimeout):
        budget.search(regex.compile(HOSTILE), HOSTILE_TEXT)
    assert time.perf_counter() - started < 0.5
    assert budget.exhausted
    # Once spent, every later call refuses at once.
    with pytest.raises(UserRegexTimeout):
        budget.search(regex.compile("a"), "a")


def test_a_per_search_bound_does_not_spend_the_whole_budget():
    budget = MatchBudget(5.0, per_search=0.01)
    with pytest.raises(UserRegexTimeout):
        budget.search(regex.compile(HOSTILE), HOSTILE_TEXT)
    assert not budget.exhausted
    assert 0.005 < budget.spent < 1.0
    assert budget.search(regex.compile("b+"), "abbb").group(0) == "bbb"


def test_finditer_runs_under_one_bound_for_the_whole_iteration():
    budget = MatchBudget(0.05)
    with pytest.raises(UserRegexTimeout):
        budget.findall_matches(regex.compile("x"), "x" * 5_000_000)
    assert MatchBudget(1.0).findall_matches(regex.compile("b"), "abcb")[1].start() == 3


def test_timed_search_answers_and_times_out():
    assert timed_search(regex.compile("b"), "abc", timeout=1.0).start() == 1
    with pytest.raises(UserRegexTimeout):
        timed_search(regex.compile(HOSTILE), HOSTILE_TEXT, timeout=0.02)


async def test_a_bounded_match_in_a_worker_thread_leaves_the_event_loop_running():
    """concurrent=True releases the GIL: the loop keeps ticking while the
    engine backtracks (stdlib re in a thread holds the GIL for the whole match)."""
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
    with pytest.raises(UserRegexTimeout):
        await asyncio.to_thread(budget.search, regex.compile(HOSTILE), HOSTILE_TEXT)
    state["stop"] = True
    await task
    assert max(gaps) < 0.2, max(gaps)
