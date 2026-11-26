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


# --- fix round 1: real UTF-8 size, and a caller-supplied size bound ---------


def test_a_multibyte_string_is_measured_as_utf8_not_ascii_escaped():
    # 330,000 CJK characters: 990,000 bytes of UTF-8 (under the 1 MiB
    # bound) but 1,980,000 bytes of ASCII-escaped JSON - the old
    # measurement (ensure_ascii=True) refused it here.
    inst = ["字" * 330_000]
    assert len(inst[0].encode()) == 990_000
    validate_bounded(inst, {"type": "array", "items": {"type": "string"}})


def test_max_instance_bytes_admits_a_larger_instance():
    # 1.5 MiB of ASCII: over the 1 MiB default bound, under a 2 MiB one.
    inst = {"q": "a" * 1_500_000}
    with pytest.raises(jsonschema.ValidationError, match="larger than 1 MiB"):
        validate_bounded(inst, {"type": "object"})
    validate_bounded(inst, {"type": "object"}, max_instance_bytes=2 * 1024 * 1024)


async def test_validate_bounded_off_loop_uses_the_passed_cap():
    inst = {"q": "a" * 1_500_000}
    await validate_bounded_off_loop(inst, {"type": "object"}, max_instance_bytes=2 * 1024 * 1024)
    with pytest.raises(jsonschema.ValidationError):
        await validate_bounded_off_loop(inst, {"type": "object"})


def test_a_spent_deadline_raises_validation_timeout():
    """Fix round 1: a deadline that expires mid-validation raises
    ``ValidationTimeout`` - a ``ValidationError`` subclass, so the
    handlers that already catch ``ValidationError`` keep reporting it as
    a violation, while a caller that can tell 'the schema is too slow'
    apart from 'the arguments are wrong' catches the subclass first."""
    from primer.common.bounded_jsonschema import ValidationTimeout

    inst = {"a": {"b": "x" * 1000}}
    schema = {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}}}}}
    with pytest.raises(ValidationTimeout) as exc:
        validate_bounded(inst, schema, seconds=0)
    assert isinstance(exc.value, jsonschema.ValidationError)


# --- fix round 2: a lone surrogate is still a string, never a UnicodeEncodeError ---


_LONE_SURROGATE = "\ud800"


def test_a_lone_surrogate_validates_like_any_string():
    inst = {"a": _LONE_SURROGATE}
    validate_bounded(inst, {"type": "object"})
    with pytest.raises(jsonschema.ValidationError):
        validate_bounded(inst, {"type": "array"})
    with pytest.raises(jsonschema.ValidationError):
        validate_bounded(inst, {"type": "object", "properties": {"a": {"type": "integer"}}})


async def test_a_lone_surrogate_off_loop_validates_like_any_string():
    inst = {"a": _LONE_SURROGATE}
    await validate_bounded_off_loop(inst, {"type": "object"})
    with pytest.raises(jsonschema.ValidationError):
        await validate_bounded_off_loop(inst, {"type": "array"})


def test_a_lone_surrogate_counts_three_bytes_toward_the_bound():
    # 3 bytes as real UTF-8 would: two of them overflow a 10 byte cap with the quotes and brackets, one does not.
    validate_bounded([_LONE_SURROGATE], {"type": "array"}, max_instance_bytes=7)
    with pytest.raises(jsonschema.ValidationError, match="larger than"):
        validate_bounded([_LONE_SURROGATE * 2], {"type": "array"}, max_instance_bytes=7)


# --- X3.6.2: the size check measures the instance's JSON without materializing it ---


def _dumps_encoded_bytes(x) -> int:
    """The size the old check compared against (dumps + encode)."""
    import json

    return len(json.dumps(x, separators=(",", ":"), ensure_ascii=False).encode("utf-8", errors="surrogatepass"))


def test_the_chunk_sum_matches_dumps_encode_on_the_hand_cases():
    # review 2026-11-23, X3.6.2: the size check now sums the stdlib encoder's
    # chunks (iterencode, never materializing the whole JSON); the total must
    # be byte-identical to the old dumps + encode measurement, on mixed
    # ASCII/CJK/escaped-control/surrogate strings, scalars (nan/inf
    # included), non-string dict keys and deep-but-within-limit nesting.
    from primer.common.bounded_jsonschema import _compact_json_size

    cases = [
        "",
        "plain ascii",
        'a"b\\c',
        "tab\there and newline\nthere",
        "\x00\x01\x1f",
        "cjk 漢字",
        "eacute émoji \U0001F680",
        "\ud800",
        "\ud83d\ude00",
        "a\ud800b漢c",
        0,
        -1,
        123456789012345678901234567890,
        0.1,
        -0.0,
        1.5e30,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        None,
        [1, "two", None, True, 3.5],
        {"k": "v", "n": 1, "f": 2.5, "b": True, "z": None},
        {"a": [1, {"b": "c"}], "d": ""},
        {1: "one", "two": 2, None: 4},
        {True: 3, False: 0},
        {"漢": "字", 7: "柒"},
    ]
    deep: object = 5
    outer = deep
    for _ in range(50):
        outer = [outer]
    cases.append(outer)
    assert len(cases) == 29
    for case in cases:
        size = _compact_json_size(case, 1 << 62)  # a bound the case cannot pass: no early exit
        assert size == _dumps_encoded_bytes(case), repr(case)[:80]


def test_the_chunk_sum_matches_dumps_encode_on_random_structures():
    import random

    from primer.common.bounded_jsonschema import _compact_json_size

    rng = random.Random(20261125)
    leaves = [None, True, False, 0, -1, 12345, 0.5, -0.25, "s", "漢字", 'a"b', "\ud800", "x\x01y", "é"]

    def node(depth: int = 0):
        if depth > 4:
            return rng.choice(leaves)
        r = rng.random()
        if r < 0.35:
            return {str(rng.randint(0, 9)): node(depth + 1) for _ in range(rng.randint(0, 4))}
        if r < 0.7:
            return [node(depth + 1) for _ in range(rng.randint(0, 4))]
        return node(depth + 1)

    for _ in range(200):
        structure = node()
        size = _compact_json_size(structure, 1 << 62)
        assert size == _dumps_encoded_bytes(structure), repr(structure)[:80]


def test_the_size_bound_is_checked_byte_exactly():
    # review 2026-11-23, X3.6.2: exactly at the bound validates; one byte
    # over refuses with the pinned message (the chunk sum keeps the old
    # measurement's boundary, no off-by-one from the early exit).
    inst = "a" * 9  # its compact JSON is the quotes plus 9: 11 bytes
    assert _dumps_encoded_bytes(inst) == 11
    validate_bounded(inst, {"type": "string"}, max_instance_bytes=11)
    with pytest.raises(
        jsonschema.ValidationError,
        match="the value is larger than 0 MiB of JSON, the most a schema validates",
    ):
        validate_bounded(inst, {"type": "string"}, max_instance_bytes=10)


def test_a_32_mib_instance_is_refused_via_the_size_path():
    # review 2026-11-23, X3.6.2: the MCP path passes the 32 MiB app cap; an
    # instance over it refuses with the pinned message via the size check
    # (not the depth check, not a RecursionError arm).
    inst = "a" * (32 * 1024 * 1024)
    with pytest.raises(
        jsonschema.ValidationError,
        match="the value is larger than 32 MiB of JSON, the most a schema validates",
    ):
        validate_bounded(inst, {"type": "string"}, max_instance_bytes=32 * 1024 * 1024)


def test_the_size_measurement_does_not_materialize_the_whole_instance():
    # review 2026-11-23, X3.6.2: the old json.dumps + encode held ~2x the
    # instance's JSON size per validation thread (the string, then its
    # UTF-8 encoding); the chunk sum peaks far below the JSON it measures.
    import tracemalloc

    from primer.common.bounded_jsonschema import _compact_json_size

    inst = ["x" * 4500 for _ in range(1000)]  # ~4.5 MB of compact JSON
    json_size = _dumps_encoded_bytes(inst)
    assert json_size > 4 * 1024 * 1024
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        size = _compact_json_size(inst, json_size + 1)
    finally:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    assert size == json_size
    assert peak < json_size // 2, (
        f"the measurement peaked at {peak} of {json_size} JSON bytes; the old dumps + encode held ~2x"
    )
