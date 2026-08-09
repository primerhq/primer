"""The functions gateway: per-request sandboxed execution of bundle python.

Reuses the python-runner wholesale (spec section 7.1): the same shim
protocol, the same hardened LocalHardenedRunner, the same timeout
semantics. What differs from a toolset call is the context (service
data only, no session/workspace) and the failure surface (typed
exceptions the router maps to RFC7807 statuses instead of tool-error
results, because the caller is an HTTP client, not an agent turn).

Yielding cannot occur here: bundles with ``@resumes`` companions are
rejected at publish (``allow_yielding=False``); a yield response at
runtime therefore indicates a bug and raises ``FunctionRaised``.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from primer.model.service import ServiceFunctionSpec, ServiceVersion
from primer.toolset.python_runner.protocol import PHASE_CALL, build_request
from primer.toolset.python_runner.runners import LocalHardenedRunner, Runner

_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024

_runner: Runner | None = None


class FunctionNotFound(Exception):
    """No such function in the active version."""


class ArgsInvalid(Exception):
    """Arguments failed the published schema."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class FunctionRaised(Exception):
    """The sandboxed function raised (or timed out, or misbehaved)."""

    def __init__(self, type_: str, message: str, traceback: str) -> None:
        super().__init__(f"{type_}: {message}")
        self.type_ = type_
        self.message = message
        self.traceback = traceback


class RunnerUnavailable(Exception):
    """The sandbox could not be started at all."""


def _get_runner() -> Runner:
    """One hardened runner per process; it holds no per-call state."""
    global _runner
    if _runner is None:
        _runner = LocalHardenedRunner(env={})
    return _runner


def find_spec(version: ServiceVersion, fn: str) -> ServiceFunctionSpec:
    for spec in version.functions:
        if spec.name == fn:
            return spec
    raise FunctionNotFound(
        f"version v{version.version} has no function named {fn!r}"
    )


def validate_args(spec: ServiceFunctionSpec, args: dict[str, Any]) -> None:
    validator = Draft202012Validator(spec.schema_)
    errors = [
        (f"{'/'.join(str(p) for p in e.path)}: " if e.path else "") + e.message
        for e in validator.iter_errors(args)
    ]
    if errors:
        raise ArgsInvalid(errors)


async def call_function(
    *,
    service_id: str,
    version: ServiceVersion,
    source: str,
    fn: str,
    args: dict[str, Any],
    runner: Runner | None = None,
) -> Any:
    """Execute one gateway function call and return its JSON value."""
    spec = find_spec(version, fn)
    validate_args(spec, args)
    request = build_request(
        module=source,
        fn=spec.name,
        phase=PHASE_CALL,
        args=args,
        ctx={
            "service_id": service_id,
            "service_version": version.version,
            # Deliberately no session/workspace/chat: a gateway call has
            # none, and the shim treats ctx as opaque data.
        },
        cpu_seconds=int(spec.timeout_seconds) + 1,
        address_space_bytes=_ADDRESS_SPACE_BYTES,
    )
    active = runner or _get_runner()
    try:
        response = await active.run(request, timeout_seconds=spec.timeout_seconds)
    except OSError as exc:
        raise RunnerUnavailable(str(exc)) from exc

    if response.yield_request is not None:
        raise FunctionRaised(
            "YieldNotAllowed",
            f"{fn!r} attempted to yield; service functions are synchronous "
            "and yielding is rejected at publish - this version should not "
            "have been published",
            "",
        )
    if not response.ok:
        err = response.error or {}
        raise FunctionRaised(
            err.get("type", "Error"),
            err.get("message", ""),
            err.get("traceback", ""),
        )
    # The shim already returns JSON-decoded values; guard round-trips for
    # exotic returns so the HTTP layer always emits valid JSON.
    value = response.value
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise FunctionRaised(
            "NonSerializableReturn",
            f"{fn!r} returned a value that is not JSON-serialisable",
            str(exc),
        ) from exc
    return value


__all__ = [
    "ArgsInvalid",
    "FunctionNotFound",
    "FunctionRaised",
    "RunnerUnavailable",
    "call_function",
    "find_spec",
    "validate_args",
]
