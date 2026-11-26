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

import jsonschema
from primer.common.bounded_jsonschema import ValidationTimeout, validate_bounded_off_loop
from primer.common.user_regex import UserRegexError, UserRegexTimeout
from primer.model.service import ServiceFunctionSpec, ServiceVersion
from primer.toolset.python_runner.protocol import PHASE_CALL, build_request
from primer.toolset.python_runner.runners import LocalHardenedRunner, Runner

_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024

# The /svc function routes take the app-wide request-body cap (LimitsConfig.max_body_bytes,
# 32 MiB; primer/api/middleware/body_limit.py), so the validator admits instances that cap:
# a body the middleware accepted must not be refused here (anonymous callers are pre-capped
# smaller by the same middleware).
_ARGS_MAX_INSTANCE_BYTES = 32 * 1024 * 1024

_runner: Runner | None = None

# Admission control (GS-8): a call on a viewer_auth=none service is anonymous and starts a python process, so the gateway admits a bounded number
# at once and answers the rest "busy" at once instead of queueing without bound. Counters, not semaphores: a refused call must not wait.
DEFAULT_GLOBAL_CAP = 32
_global_cap = DEFAULT_GLOBAL_CAP
_per_service_cap: int | None = None
_running = 0
_running_by_service: dict[str, int] = {}


def configure_limits(*, global_cap: int, per_service_cap: int | None) -> None:
    """Set the caps (the app lifespan calls this from ``limits``); ``per_service_cap=None`` means no per-service cap."""
    global _global_cap, _per_service_cap
    _global_cap, _per_service_cap = global_cap, per_service_cap


class GatewayBusy(Exception):
    """The gateway is at its concurrency cap; the caller should retry shortly."""


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


def _admit(service_id: str) -> None:
    global _running
    if _running >= _global_cap:
        raise GatewayBusy(f"{_running} calls are running (cap {_global_cap})")
    if _per_service_cap is not None and _running_by_service.get(service_id, 0) >= _per_service_cap:
        raise GatewayBusy(f"service {service_id} has {_per_service_cap} calls running")
    _running += 1
    _running_by_service[service_id] = _running_by_service.get(service_id, 0) + 1


def _release(service_id: str) -> None:
    global _running
    _running -= 1
    left = _running_by_service.get(service_id, 1) - 1
    if left > 0:
        _running_by_service[service_id] = left
    else:
        _running_by_service.pop(service_id, None)


def find_spec(version: ServiceVersion, fn: str) -> ServiceFunctionSpec:
    for spec in version.functions:
        if spec.name == fn:
            return spec
    raise FunctionNotFound(
        f"version v{version.version} has no function named {fn!r}"
    )


def _format_validation_error(error: jsonschema.ValidationError) -> str:
    """The pre-7.1b error string: "<path>: <message>", no prefix for a top-level error."""
    prefix = f"{'/'.join(str(p) for p in error.path)}: " if error.path else ""
    return prefix + error.message


async def validate_args(spec: ServiceFunctionSpec, args: dict[str, Any]) -> None:
    """Validate call arguments against the published schema, off the event loop.

    Bounded (7.1b): the published schema is operator data, and the
    jsonschema package on the loop could hang the loop (a backtracking
    pattern, a uniqueItems explosion). The bounded validator runs in a
    worker thread
    with per-regex and whole-validation budgets, and a budget that expires
    refuses the call instead of blocking it.
    """
    try:
        await validate_bounded_off_loop(
            args, spec.schema_,
            label=f"service {spec.name} arguments schema",
            max_instance_bytes=_ARGS_MAX_INSTANCE_BYTES,
        )
    except ValidationTimeout as exc:
        # The whole-validation deadline expired: fail closed, and name the
        # cause so a caller is not told their arguments are simply wrong.
        raise ArgsInvalid([
            f"the published arguments schema for {spec.name!r} exceeded its validation "
            f"budget and the call was refused: {exc.message}"
        ]) from exc
    except (UserRegexTimeout, UserRegexError) as exc:
        raise ArgsInvalid([
            f"the published arguments schema for {spec.name!r} cannot be validated safely "
            f"and the call was refused: {exc}"
        ]) from exc
    except jsonschema.ValidationError as exc:
        # Ordinary invalid input keeps the pre-7.1b error string (one best-match
        # entry where the old code listed every error; the 422 envelope is unchanged).
        raise ArgsInvalid([_format_validation_error(exc)]) from exc


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
    # Admit before validating: the argument check runs on a worker thread, so a
    # burst admitted after it would queue validation work for every call before
    # the cap refused any. Admission first refuses the burst at once, and the
    # slot also bounds the validations in flight.
    _admit(service_id)
    try:
        await validate_args(spec, args)
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
    finally:
        _release(service_id)

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
