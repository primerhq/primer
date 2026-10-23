"""primer.common.bounded_jsonschema: every jsonschema keyword that runs a regex is bounded inside validate_bounded (G6-4).

jsonschema reaches ``re.search`` from four keywords: ``pattern``, ``patternProperties``, ``additionalProperties`` (which joins
the patternProperties keys) and ``unevaluatedProperties`` (which consults them while collecting evaluated keys). Each test below
puts a catastrophic pattern on one of those routes; if a jsonschema release stopped reading ``re`` through the patched modules,
the route would run unbounded and the test would fail on time.
"""

from __future__ import annotations

import asyncio
import re
import time

import jsonschema
import pytest

from primer.common.bounded_jsonschema import validate_bounded, validate_bounded_off_loop
from primer.common.user_regex import UserRegexError, UserRegexTimeout

HOSTILE = r"(a|aa)+$"  # exponential in both engines: tens of seconds on these strings unbounded
LONG = "a" * 60 + "!"

ROUTES = {
    "pattern": ({"type": "string", "pattern": HOSTILE}, LONG),
    "patternProperties": ({"type": "object", "patternProperties": {HOSTILE: {"type": "string"}}}, {LONG: "x"}),
    # The keyword under test comes first: jsonschema runs a schema's keywords in order, so it is the one that meets the
    # pattern first (an unbounded route would hang there).
    "additionalProperties": (
        {"additionalProperties": False, "type": "object", "properties": {"k": {}}, "patternProperties": {HOSTILE: {}}},
        {LONG: 1},
    ),
    "unevaluatedProperties": (
        {"unevaluatedProperties": False, "type": "object", "patternProperties": {HOSTILE: {}}},
        {LONG: 1},
    ),
    "legacy-draft-2019": (
        {"$schema": "https://json-schema.org/draft/2019-09/schema", "unevaluatedProperties": False,
         "type": "object", "patternProperties": {HOSTILE: {}}},
        {LONG: 1},
    ),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_every_regex_route_is_bounded(route):
    schema, instance = ROUTES[route]
    started = time.perf_counter()
    with pytest.raises(UserRegexTimeout):
        validate_bounded(instance, schema, seconds=0.2)
    assert time.perf_counter() - started < 1.5


def test_answers_are_unchanged_for_ordinary_patterns():
    schema = {
        "type": "object",
        "properties": {"id": {"type": "string", "pattern": r"^[a-z]+-\d+$"}},
        "patternProperties": {r"^x-": {"type": "integer"}},
        "additionalProperties": False,
    }
    validate_bounded({"id": "abc-12", "x-count": 3}, schema)
    with pytest.raises(jsonschema.ValidationError, match="does not match"):
        validate_bounded({"id": "ABC"}, schema)
    with pytest.raises(jsonschema.ValidationError, match="not of type 'integer'"):
        validate_bounded({"x-count": "three"}, schema)
    with pytest.raises(jsonschema.ValidationError, match="does not match any of the regexes"):
        validate_bounded({"other": 1}, schema)


def test_a_schema_pattern_over_the_size_caps_is_refused():
    with pytest.raises(UserRegexError, match="the limit is 1000"):
        validate_bounded("x", {"type": "string", "pattern": "x" * 1001})


def test_other_jsonschema_callers_keep_the_stdlib_engine():
    # Outside validate_bounded nothing changes: the stdlib engine runs, unbounded as before (a cheap pattern here).
    from jsonschema import _keywords

    assert _keywords.re.search(r"\d", "a1").group(0) == "1"
    assert isinstance(_keywords.re.search(r"\d", "a1"), re.Match)
    jsonschema.validate("abc-1", {"type": "string", "pattern": r"^[a-z]+-\d$"})


async def test_the_off_loop_form_leaves_the_event_loop_running():
    gaps = [0.0]
    state = {"stop": False}

    async def ticker() -> None:
        last = time.perf_counter()
        while not state["stop"]:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    schema, instance = ROUTES["pattern"]
    with pytest.raises(UserRegexTimeout):
        await validate_bounded_off_loop(instance, schema, seconds=0.4)
    state["stop"] = True
    await task
    assert max(gaps) < 0.2, max(gaps)
