"""Built-in ``misc`` toolset — small portable utilities for agents.

Catch-all for cheap, side-effect-free helpers that LLMs often want but
can't reliably compute themselves: current time, controlled pacing,
stable id generation, content hashing, and arithmetic. Like
``system`` and ``workspaces``, ``misc`` is reserved (its toolset id
short-circuits the normal ``Toolset`` row lookup in
:class:`primer.api.registries.ProviderRegistry`) and built once at
app startup.

Tool catalog
------------

* ``get_datetime`` — current date/time as ISO 8601 + Unix epoch.
  Optional ``timezone`` (IANA name, default UTC).
* ``inform_user`` - push a one-way status message to the operator
  (non-yielding); returns ``{delivered_to}``.
* ``uuid_v4`` — generate one or more random UUIDs. Agents should
  reach for this whenever they need a stable identifier rather than
  fabricating one (LLM-generated "random" strings are low-entropy).
* ``hash`` — hex digest of a string under sha256/sha1/md5. Useful
  for content-addressing and dedup checks.
* ``calculate`` — safe arithmetic expression evaluator. Supports
  +, -, *, /, //, %, **, parentheses, unary +/-, an allowlist of
  math functions (abs, round, min, max, pow, sqrt, log, log2, log10,
  exp, sin, cos, tan, asin, acos, atan, floor, ceil), and the
  constants pi, e, tau. NOT a Python eval — no attribute access,
  no comprehensions, no string ops, no imports.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import math
import operator
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok_json as _ok
from primer.toolset.internal import InternalToolsetProvider, ToolHandler


logger = logging.getLogger(__name__)


MISC_TOOLSET_ID = "misc"


# ===========================================================================
# Helpers - uniform OK / error wrapping (``_ok`` / ``_err`` are the shared
# toolset result builders, imported above from ``primer.toolset._helpers``).
# ===========================================================================


def _err_from_validation(exc: ValidationError) -> ToolCallResult:
    return _err(
        "argument validation failed: " + json.dumps(exc.errors(), default=str),
        error_type="validation-error",
    )


# ===========================================================================
# get_datetime
# ===========================================================================


class _GetDatetimeArgs(BaseModel):
    """Optional timezone selector for the returned datetime."""

    timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone name (e.g. 'UTC', 'America/Los_Angeles', "
            "'Asia/Tokyo'). Defaults to UTC. Invalid names return "
            "``is_error=true`` ``type=bad-request``."
        ),
    )


async def _get_datetime_handler(arguments: dict[str, Any]) -> ToolCallResult:
    try:
        args = _GetDatetimeArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)

    tz = timezone.utc
    tz_name = "UTC"
    if args.timezone is not None and args.timezone != "UTC":
        # zoneinfo lookup; reject unknown names cleanly
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            tz = ZoneInfo(args.timezone)
            tz_name = args.timezone
        except ZoneInfoNotFoundError as exc:
            return _err(
                f"unknown timezone {args.timezone!r}: {exc}",
                error_type="bad-request",
            )

    now = datetime.now(tz)
    return _ok(
        {
            "datetime": now.isoformat(),
            "timezone": tz_name,
            "unix": now.timestamp(),
        }
    )


# ===========================================================================
# sleep — first yielding tool (M1 of the yielding-tools feature).
# See docs/superpowers/specs/2026-05-22-yielding-tools-design.md §8.1.
#
# Returns a Yielded sentinel; the worker parks the session and the
# timer scheduler (M2) republishes on parked_until expiry. Resume
# synthesises the elapsed time from parked_at. The cap moves to the
# global yield timeout (default 60 min) — sleep no longer enforces
# its own 300s ceiling.
# ===========================================================================


class _SleepArgs(BaseModel):
    """How long to pause the calling agent's turn for."""

    seconds: float = Field(
        ...,
        ge=0.0,
        description=(
            "Number of seconds to sleep. Fractional values are "
            "honoured. Bounded by the global yield-timeout cap "
            "(default 60 minutes). Longer waits are documented as "
            "a future Yielding-Tools follow-up rather than today's "
            "tool surface."
        ),
    )


def sleep_resume(yield_metadata: dict[str, Any], event_payload: Any) -> ToolCallResult:
    """Resume hook for sleep — synthesise elapsed-seconds.

    Sleep's event payload is empty (the timer scheduler just fires);
    the resume hook reads ``requested_seconds`` from the metadata and
    computes elapsed from the parked_at timestamp the worker injected.
    On YieldCancelled the elapsed time is similarly computed; the
    tool result reports both ``cancelled`` and the actual elapsed
    seconds so the agent can decide whether to redo the wait.

    The runtime imports this lazily (via the registry) so M1's
    sleep migration doesn't create a cycle between misc.py and the
    worker yield_runtime module.
    """
    from primer.model.yield_ import YieldCancelled  # local: avoid cycle.
    requested = float(yield_metadata.get("requested_seconds", 0.0))
    # Worker injects parked_at_iso into yield_metadata on resume —
    # see worker/yield_runtime.py for the convention.
    parked_at_iso = yield_metadata.get("parked_at_iso")
    elapsed = 0.0
    if parked_at_iso:
        parked_at = datetime.fromisoformat(parked_at_iso)
        elapsed = (datetime.now(timezone.utc) - parked_at).total_seconds()
    base = {
        "requested_seconds": requested,
        "elapsed_seconds": elapsed,
    }
    if isinstance(event_payload, YieldCancelled):
        base["cancelled"] = True
        base["cancel_reason"] = event_payload.reason
    return _ok(base)


async def _sleep_handler(
    arguments: dict[str, Any],
    *,
    ctx: ToolContext,
) -> ToolCallResult | Yielded:
    try:
        args = _SleepArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)

    # Zero-second sleeps short-circuit: no point parking, no event
    # to wait for. Return the result directly so the LLM loop
    # continues without crossing the worker boundary.
    if args.seconds == 0.0:
        return _ok({"requested_seconds": 0.0, "elapsed_seconds": 0.0})

    # The Yielded sentinel; the provider stamps tool_name onto it
    # and raises YieldToWorker. The worker writes parked_state with
    # the rehydrated parked_at_iso so the resume hook can compute
    # elapsed.
    return Yielded(
        tool_name="",  # filled in by the provider; placeholder
        event_key=f"timer:{ctx.tool_call_id}",
        timeout=args.seconds,
        resume_metadata={
            "requested_seconds": args.seconds,
        },
    )


# ===========================================================================
# inform_user — non-yielding one-way message to the operator.
#
# Unlike ask_user (which parks the turn awaiting a reply), inform_user
# relays a one-way status update via ctx.inform and returns
# immediately. The handler returns ToolCallResult ONLY (no Yielded),
# so the provider classifies it non-yielding.
# ===========================================================================


class _InformUserArgs(BaseModel):
    """Message delivered one-way to the operator (no reply awaited)."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description=(
            "Message delivered to the operator. Newlines preserved. No reply "
            "is awaited; the agent continues immediately."
        ),
    )
    files: list[str] | None = Field(
        default=None,
        description=(
            "Optional workspace-relative file paths to attach. Each is read "
            "from the session's workspace, stored, and sent to the channel as "
            "media alongside the message. Ignored on the chat surface."
        ),
    )


async def _inform_user_handler(
    arguments: dict[str, Any],
    *,
    ctx: ToolContext | None = None,
) -> ToolCallResult:
    # ctx is optional + None-safe: inform_user is non-yielding and thus
    # MCP-eligible, where the provider dispatches with ctx=None. No context
    # means no delivery channel, so it degrades to delivered_to: 0.
    try:
        args = _InformUserArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)
    sink = getattr(ctx, "inform", None)
    if sink is None:
        return _ok({"delivered_to": 0})
    # Pass files only when present so simpler sinks (no files support) keep
    # their one-arg signature.
    if args.files:
        delivered = await sink(args.message, files=args.files)
    else:
        delivered = await sink(args.message)
    return _ok({"delivered_to": int(delivered)})


# ===========================================================================
# uuid_v4
# ===========================================================================


class _UuidV4Args(BaseModel):
    """How many UUIDs to generate."""

    count: int = Field(
        default=1,
        ge=1,
        le=100,
        description=(
            "Number of UUIDv4 strings to generate (1-100, default 1). "
            "Each is a fresh random uuid produced by the OS CSPRNG."
        ),
    )


async def _uuid_v4_handler(arguments: dict[str, Any]) -> ToolCallResult:
    try:
        args = _UuidV4Args.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)
    uuids = [str(uuid.uuid4()) for _ in range(args.count)]
    return _ok({"uuids": uuids})


# ===========================================================================
# hash
# ===========================================================================


class _HashArgs(BaseModel):
    """Compute a hex digest of an input string."""

    input: str = Field(
        ...,
        description=(
            "String to hash. Encoded as UTF-8 before digesting. "
            "Empty string is allowed."
        ),
    )
    algorithm: Literal["sha256", "sha1", "md5"] = Field(
        default="sha256",
        description=(
            "Digest algorithm. ``sha256`` (default) for content "
            "addressing; ``sha1`` and ``md5`` available for "
            "interop with legacy systems — neither is "
            "cryptographically safe."
        ),
    )


async def _hash_handler(arguments: dict[str, Any]) -> ToolCallResult:
    try:
        args = _HashArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)
    digest = hashlib.new(args.algorithm, args.input.encode("utf-8")).hexdigest()
    return _ok({"algorithm": args.algorithm, "hex_digest": digest})


# ===========================================================================
# calculate — safe arithmetic expression evaluator
# ===========================================================================


_BINOPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARYOPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_FUNCTIONS: dict[str, Any] = {
    "abs": abs, "round": round, "min": min, "max": max, "pow": pow,
    "sqrt": math.sqrt, "log": math.log, "log2": math.log2,
    "log10": math.log10, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "floor": math.floor, "ceil": math.ceil,
}

_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


def _safe_eval(node: ast.AST) -> Any:
    """Walk a single expression node, allowing only arithmetic +
    allowlisted function calls. Raises ``ValueError`` on anything
    outside the allowlist (attribute access, subscript, comprehension,
    string ops, etc.)."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(
            f"unsupported literal: {node.value!r} (only int/float)"
        )
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ValueError(
                f"unsupported binary operator: {type(node.op).__name__}"
            )
        return op(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARYOPS.get(type(node.op))
        if op is None:
            raise ValueError(
                f"unsupported unary operator: {type(node.op).__name__}"
            )
        return op(_safe_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError(
                "only direct function calls allowed (no attribute access)"
            )
        fn = _FUNCTIONS.get(node.func.id)
        if fn is None:
            raise ValueError(
                f"unknown function {node.func.id!r}; allowed: "
                f"{sorted(_FUNCTIONS)!r}"
            )
        if node.keywords:
            raise ValueError("keyword arguments not allowed")
        return fn(*[_safe_eval(a) for a in node.args])
    if isinstance(node, ast.Name):
        value = _CONSTANTS.get(node.id)
        if value is None:
            raise ValueError(
                f"unknown name {node.id!r}; allowed constants: "
                f"{sorted(_CONSTANTS)!r}"
            )
        return value
    raise ValueError(
        f"unsupported expression: {type(node).__name__}"
    )


class _CalculateArgs(BaseModel):
    """Arithmetic expression to evaluate."""

    expression: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description=(
            "Arithmetic expression. Supports +, -, *, /, //, %, **, "
            "parentheses, unary +/-. Allowed functions: "
            "abs, round, min, max, pow, sqrt, log, log2, log10, exp, "
            "sin, cos, tan, asin, acos, atan, floor, ceil. Allowed "
            "constants: pi, e, tau. NO Python attribute access, "
            "subscripts, comprehensions, or string operations. "
            "Examples: '2 + 2', 'sqrt(16) * pi', "
            "'(100 - 32) * 5 / 9'."
        ),
    )


async def _calculate_handler(arguments: dict[str, Any]) -> ToolCallResult:
    try:
        args = _CalculateArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)
    try:
        tree = ast.parse(args.expression, mode="eval")
    except SyntaxError as exc:
        return _err(
            f"syntax error in expression: {exc.msg}",
            error_type="bad-request",
        )
    try:
        result = _safe_eval(tree)
    except ValueError as exc:
        return _err(str(exc), error_type="bad-request")
    except (ZeroDivisionError, OverflowError) as exc:
        return _err(
            f"evaluation error: {type(exc).__name__}: {exc}",
            error_type="bad-request",
        )
    # Coerce result to a JSON-friendly number; reject non-finite values
    if isinstance(result, float) and not math.isfinite(result):
        return _err(
            f"non-finite result: {result}",
            error_type="bad-request",
        )
    return _ok({"expression": args.expression, "result": result})


# ===========================================================================
# Build the toolset
# ===========================================================================


def build_misc_toolset(
    *, toolset_id: str = MISC_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the immutable ``_misc`` toolset.

    Stateless — takes no registries. The tools depend only on the
    standard library, so this can be built unconditionally at app
    startup with no other wiring.
    """
    registry: dict[str, tuple[Tool, ToolHandler]] = {
        "get_datetime": (
            make_tool(
                id="get_datetime",
                toolset_id=toolset_id,
                purpose=(
                    "Return the current wall-clock date and time as ISO 8601 "
                    "plus Unix epoch seconds (``{datetime, timezone, unix}``)."
                ),
                when=(
                    "Use when you need the real current time; do not estimate "
                    "or reuse a time from earlier in the conversation."
                ),
                args_schema=_GetDatetimeArgs.model_json_schema(),
                examples=[
                    ToolExample(args={}, returns="now in UTC"),
                    ToolExample(
                        args={"timezone": "America/New_York"},
                        returns="same instant in US Eastern",
                    ),
                ],
                required_role="user",
            ),
            _get_datetime_handler,
        ),
        "inform_user": (
            make_tool(
                id="inform_user",
                toolset_id=toolset_id,
                purpose=(
                    "Send the human operator a one-way message (status update, "
                    "progress note) and keep going; returns "
                    "``{delivered_to: <int>}``. Does NOT wait for a reply."
                ),
                when=(
                    "Use when you want to keep the operator informed mid-task "
                    "without blocking; when you need an answer or a decision, "
                    "use ``ask_user`` instead."
                ),
                args_schema=_InformUserArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={"message": "Starting the migration now (~5 min)."},
                        returns="{delivered_to: N}",
                        note="non-yielding; one-way",
                    ),
                ],
                required_role="user",
            ),
            _inform_user_handler,
        ),
        "uuid_v4": (
            make_tool(
                id="uuid_v4",
                toolset_id=toolset_id,
                purpose=(
                    "Generate one or more cryptographically random UUIDv4 "
                    "strings; returns ``{uuids: [...]}``."
                ),
                when=(
                    "Use when you need a fresh stable identifier (entity id, "
                    "conversation tag, dedup key); do not invent one yourself, "
                    "LLM-generated 'random' strings are low entropy."
                ),
                args_schema=_UuidV4Args.model_json_schema(),
                examples=[
                    ToolExample(args={"count": 1}, returns="one UUIDv4"),
                    ToolExample(args={"count": 3}, returns="three UUIDs"),
                ],
                required_role="user",
            ),
            _uuid_v4_handler,
        ),
        "hash": (
            make_tool(
                id="hash",
                toolset_id=toolset_id,
                purpose=(
                    "Compute a hex digest of an input string under "
                    "sha256/sha1/md5; returns ``{algorithm, hex_digest}``."
                ),
                when=(
                    "Use when you need content addressing or a dedup check; "
                    "sha256 (default) is for content addressing, sha1 and md5 "
                    "are for interop only."
                ),
                args_schema=_HashArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={"input": "hello"},
                        returns="sha256 hex digest",
                    ),
                    ToolExample(
                        args={"input": "hello", "algorithm": "md5"},
                        returns="md5 hex (interop only)",
                    ),
                ],
                required_role="user",
            ),
            _hash_handler,
        ),
        "calculate": (
            make_tool(
                id="calculate",
                toolset_id=toolset_id,
                purpose=(
                    "Evaluate an arithmetic expression safely (``+ - * / // % "
                    "**``, parentheses, an allowlist of math functions, and "
                    "the constants pi, e, tau); returns ``{expression, "
                    "result}``."
                ),
                when=(
                    "Use when you need correct arithmetic; do not compute by "
                    "hand. NOT a Python eval: no attribute access, no "
                    "comprehensions, no string ops."
                ),
                args_schema=_CalculateArgs.model_json_schema(),
                examples=[
                    ToolExample(args={"expression": "2 + 2 * 10"}, returns="22"),
                    ToolExample(
                        args={"expression": "sqrt(144)"},
                        returns="12.0",
                    ),
                ],
                required_role="user",
            ),
            _calculate_handler,
        ),
    }

    logger.info(
        "misc toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )

    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


# Register yielding-tool resume hooks at import time. The worker's
# resume path looks up hooks by tool name from this central
# registry — see primer/worker/yield_resume_registry.py.
from primer.worker.yield_resume_registry import register_resume_hook  # noqa: E402

register_resume_hook("sleep", sleep_resume)


__all__ = ["MISC_TOOLSET_ID", "build_misc_toolset"]
