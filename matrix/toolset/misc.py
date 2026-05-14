"""Built-in ``_misc`` toolset — small portable utilities for agents.

Catch-all for cheap, side-effect-free helpers that LLMs often want but
can't reliably compute themselves: current time, controlled pacing,
stable id generation, content hashing, and arithmetic. Like
``_system`` and ``_workspaces``, ``_misc`` is reserved (its toolset id
short-circuits the normal ``Toolset`` row lookup in
:class:`matrix.api.registries.ProviderRegistry`) and built once at
app startup.

Tool catalog
------------

* ``get_datetime`` — current date/time as ISO 8601 + Unix epoch.
  Optional ``timezone`` (IANA name, default UTC).
* ``sleep`` — pause for ``seconds`` (0–300). Useful for polling and
  deliberate pacing. Returns the actual elapsed time.
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

import asyncio
import ast
import hashlib
import json
import logging
import math
import operator
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from matrix.model.chat import Tool, ToolCallResult
from matrix.toolset.internal import InternalToolsetProvider, ToolHandler


logger = logging.getLogger(__name__)


MISC_TOOLSET_ID = "_misc"


# ===========================================================================
# Helpers — uniform OK / error wrapping (mirrors system.py)
# ===========================================================================


def _ok(payload: Any) -> ToolCallResult:
    return ToolCallResult(
        output=json.dumps(payload, default=str), is_error=False,
    )


def _err(message: str, *, error_type: str = "tool-error") -> ToolCallResult:
    return ToolCallResult(
        output=json.dumps({"type": error_type, "message": message}),
        is_error=True,
    )


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
# sleep
# ===========================================================================


_SLEEP_MAX_SECONDS = 300.0


class _SleepArgs(BaseModel):
    """How long to pause the calling agent's turn for."""

    seconds: float = Field(
        ...,
        ge=0.0,
        le=_SLEEP_MAX_SECONDS,
        description=(
            "Number of seconds to sleep, in [0, 300]. Fractional "
            "values are honoured. The cap exists to prevent agents "
            "from accidentally pausing for hours; if you need a "
            "longer wait, sleep multiple times in a loop."
        ),
    )


async def _sleep_handler(arguments: dict[str, Any]) -> ToolCallResult:
    try:
        args = _SleepArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)
    started = time.monotonic()
    await asyncio.sleep(args.seconds)
    elapsed = time.monotonic() - started
    return _ok({"requested_seconds": args.seconds, "elapsed_seconds": elapsed})


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
    except (ZeroDivisionError, OverflowError, ValueError) as exc:
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
            Tool(
                id="get_datetime",
                description=(
                    "Current date and time as ISO 8601 plus Unix "
                    "epoch seconds. Optional ``timezone`` (IANA name, "
                    "e.g. 'America/New_York') — defaults to UTC. "
                    "Returns ``{datetime, timezone, unix}``. "
                    "Use this whenever you need the wall-clock time; "
                    "do not estimate."
                ),
                toolset_id=toolset_id,
                schema=_GetDatetimeArgs.model_json_schema(),
            ),
            _get_datetime_handler,
        ),
        "sleep": (
            Tool(
                id="sleep",
                description=(
                    "Pause this agent turn for ``seconds`` seconds "
                    "(fractional allowed; capped at 300). Useful for "
                    "polling external state with backoff or for "
                    "deliberate pacing between actions. Returns "
                    "``{requested_seconds, elapsed_seconds}``."
                ),
                toolset_id=toolset_id,
                schema=_SleepArgs.model_json_schema(),
            ),
            _sleep_handler,
        ),
        "uuid_v4": (
            Tool(
                id="uuid_v4",
                description=(
                    "Generate one or more cryptographically random "
                    "UUIDv4 strings. Use this whenever you need a "
                    "fresh stable identifier (entity id, conversation "
                    "tag, dedup key) — do not invent one yourself, "
                    "LLM-generated 'random' strings are low entropy. "
                    "Returns ``{uuids: [...]}``."
                ),
                toolset_id=toolset_id,
                schema=_UuidV4Args.model_json_schema(),
            ),
            _uuid_v4_handler,
        ),
        "hash": (
            Tool(
                id="hash",
                description=(
                    "Compute a hex digest of an input string. Default "
                    "algorithm is sha256 (suitable for content "
                    "addressing); sha1 and md5 are available for "
                    "interop only — neither is cryptographically "
                    "safe. Returns ``{algorithm, hex_digest}``."
                ),
                toolset_id=toolset_id,
                schema=_HashArgs.model_json_schema(),
            ),
            _hash_handler,
        ),
        "calculate": (
            Tool(
                id="calculate",
                description=(
                    "Evaluate an arithmetic expression safely. "
                    "Supports +, -, *, /, //, %, **, parentheses, "
                    "unary +/-, an allowlist of math functions "
                    "(abs, round, min, max, pow, sqrt, log, log2, "
                    "log10, exp, sin, cos, tan, asin, acos, atan, "
                    "floor, ceil), and the constants pi, e, tau. "
                    "NOT a Python eval — no attribute access, no "
                    "comprehensions, no string ops. Use this "
                    "whenever you need correct arithmetic; do not "
                    "compute by hand. Returns "
                    "``{expression, result}``."
                ),
                toolset_id=toolset_id,
                schema=_CalculateArgs.model_json_schema(),
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


__all__ = ["MISC_TOOLSET_ID", "build_misc_toolset"]
