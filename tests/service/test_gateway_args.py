"""Gateway argument validation: bounded and off the event loop (7.1b).

The published schema is operator data. The old code validated it with the
jsonschema package inline on the loop, where a backtracking pattern (or a
uniqueItems
explosion) held the loop for the whole match. The new code runs the bounded
validator in a worker thread; the loop keeps serving, and a budget that
expires refuses the call instead of blocking it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from primer.model.service import ServiceFunctionSpec
from primer.service import gateway

# The house hostile pattern (tests/common/test_bounded_jsonschema.py):
# exponential in the stdlib engine, well past the 1 s regex budget.
HOSTILE = r"(a|aa)+$"
LONG = "a" * 60 + "!"


def _spec(schema: dict, name: str = "f") -> ServiceFunctionSpec:
    return ServiceFunctionSpec(
        name=name, schema=schema, timeout_seconds=5.0, source_file="functions.py",
    )


def _legacy_errors(schema: dict, args: dict) -> list[str]:
    """The pre-7.1b strings: jsonschema's Draft202012Validator, "<path>: <message>"."""
    from jsonschema import Draft202012Validator

    return [
        (f"{'/'.join(str(p) for p in e.path)}: " if e.path else "") + e.message
        for e in Draft202012Validator(schema).iter_errors(args)
    ]


async def test_a_backtracking_pattern_refuses_promptly_without_holding_the_loop():
    spec = _spec(
        {"type": "object", "properties": {"s": {"type": "string", "pattern": HOSTILE}}, "required": ["s"]},
    )
    gaps = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.perf_counter()
        while not stop.is_set():
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    started = time.perf_counter()
    try:
        with pytest.raises(gateway.ArgsInvalid) as exc:
            await gateway.validate_args(spec, {"s": LONG})
    finally:
        stop.set()
        await beat
    elapsed = time.perf_counter() - started
    # The loop never stalled across a full heartbeat interval: validation ran
    # off it. The old inline code stalled for the whole match (minutes here).
    assert max(gaps) < 1.0, max(gaps)
    assert elapsed < 5
    assert any("cannot be validated safely" in e for e in exc.value.errors), exc.value.errors


async def test_ordinary_invalid_input_keeps_its_message():
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    spec = _spec(schema)
    # Single-error cases: the new best-match entry is exactly the old string.
    for args in ({"x": "not-an-int"}, {}):
        with pytest.raises(gateway.ArgsInvalid) as exc:
            await gateway.validate_args(spec, args)
        assert exc.value.errors == _legacy_errors(schema, args), exc.value.errors


async def test_valid_arguments_pass():
    spec = _spec({"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]})
    await gateway.validate_args(spec, {"x": 1})  # no exception


async def test_call_function_validates_before_it_runs():
    # The production caller path: an invalid argument must refuse before any
    # runner work happens, with the same 422-facing exception.
    class _NoRunner:
        async def run(self, request, *, timeout_seconds):
            raise AssertionError("the runner must not start for invalid arguments")

    spec = _spec({"type": "object", "properties": {"x": {"type": "integer"}}})
    from primer.model.service import ServiceManifest, ServiceVersion

    version = ServiceVersion(
        service_id="service-1", version=1, manifest=ServiceManifest(),
        files={}, functions=[spec],
    )
    with pytest.raises(gateway.ArgsInvalid) as exc:
        await gateway.call_function(
            service_id="service-1", version=version, source="def f(x: int) -> int:\n    return x",
            fn="f", args={"x": "not-an-int"}, runner=_NoRunner(),
        )
    assert exc.value.errors == _legacy_errors(spec.schema_, {"x": "not-an-int"})
