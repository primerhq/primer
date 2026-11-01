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


# ---- #859 round 3: R2-9 (the budget before and around compiling), R2-3 (the interactive executor) -----------------------


def test_a_spent_budget_refuses_before_compiling_a_schema_pattern(monkeypatch):
    import primer.common.bounded_jsonschema as bounded

    compiled: list[str] = []
    monkeypatch.setattr(bounded, "compile_user_regex", lambda p, **kw: compiled.append(p))
    with pytest.raises(UserRegexTimeout):
        validate_bounded("abc", {"type": "string", "pattern": "^never-compiled$"}, seconds=0.0)
    assert compiled == []


def test_compile_time_is_charged_to_the_validation_budget(monkeypatch):
    import primer.common.bounded_jsonschema as bounded

    real = bounded.compile_user_regex

    def _slow(pattern, **kwargs):
        time.sleep(0.05)
        return real(pattern, **kwargs)

    monkeypatch.setattr(bounded, "compile_user_regex", _slow)
    # A trivial pattern: only the charged compile can spend this 30 ms budget.
    with pytest.raises(UserRegexTimeout):
        validate_bounded("abc", {"type": "string", "pattern": "^charged-compile-[a-z]+$"}, seconds=0.03)


async def test_output_schema_validation_runs_on_the_interactive_executor(monkeypatch):
    import threading

    import primer.common.bounded_jsonschema as bounded

    names: list[str] = []
    real = bounded.validate_bounded

    def _recording(*args, **kwargs):
        names.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(bounded, "validate_bounded", _recording)
    await validate_bounded_off_loop("abc", {"type": "string", "pattern": "^a"})
    assert names and names[0].startswith("user-regex-interactive"), names


# ---- Part 3 Task 109 (GS-17): the whole validation is bounded, not only its regexes ----------------------------------


def _counting(monkeypatch, owner, name: str) -> list[int]:
    calls = [0]
    real = getattr(owner, name)

    def counted(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return calls


async def test_unique_items_over_many_objects_stops_at_the_budget(monkeypatch):
    from jsonschema import _utils

    from primer.graph._node_refs import _schema_violation

    calls = _counting(monkeypatch, _utils, "equal")
    n = 3000
    violation = await _schema_violation([{"a": i} for i in range(n)], {"type": "array", "uniqueItems": True})
    assert violation is not None and "budget" in violation
    assert calls[0] < n * (n - 1) // 2      # stopped part way through the pairwise comparison


async def test_a_schema_that_branches_at_every_level_stops_at_the_budget(monkeypatch):
    from primer.graph._node_refs import _schema_violation

    calls = _counting(monkeypatch, jsonschema.Draft202012Validator, "descend")
    both = {"anyOf": [{"required": ["a"], "properties": {"c": {"$ref": "#"}}},
                      {"required": ["b"], "properties": {"c": {"$ref": "#"}}}]}
    inst: dict = {}
    cur = inst
    for _ in range(40):
        cur["c"] = {}
        cur = cur["c"]
    violation = await _schema_violation(inst, both)
    assert violation is not None and "budget" in violation
    assert calls[0] < 2 ** 40


async def test_a_deep_instance_is_a_violation_not_a_recursion_error():
    from primer.graph._node_refs import _schema_violation

    inst: dict = {}
    cur = inst
    for _ in range(300):
        cur["c"] = {}
        cur = cur["c"]
    violation = await _schema_violation(inst, {"type": "object", "properties": {"c": {"$ref": "#"}}})
    assert violation == "the value nests deeper than 64 levels, the most a schema validates"


def test_an_instance_at_the_depth_cap_still_validates():
    inst: dict = {}
    cur = inst
    for _ in range(63):
        cur["c"] = {}
        cur = cur["c"]
    validate_bounded(inst, {"type": "object", "properties": {"c": {"$ref": "#"}}})


async def test_an_instance_over_1_mib_is_a_violation():
    from primer.graph._node_refs import _schema_violation

    assert await _schema_violation(["x" * 1024] * 1100, {"type": "array"}) == (
        "the value is larger than 1 MiB of JSON, the most a schema validates"
    )


async def test_a_recursion_error_is_a_violation():
    from primer.graph._node_refs import _schema_violation

    schema: dict = {"type": "array"}
    for _ in range(3000):
        schema = {"items": schema}
    assert await _schema_violation([[1]], schema) == "the schema or the value nests too deeply to validate"
