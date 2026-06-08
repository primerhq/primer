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
from primer.toolset.internal import InternalToolsetProvider, ToolHandler


logger = logging.getLogger(__name__)


MISC_TOOLSET_ID = "misc"


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
# ask_user — second yielding tool (M3 of the yielding-tools feature).
# See docs/superpowers/specs/2026-05-22-yielding-tools-design.md §8.2.
#
# Pauses the agent's turn until a human operator types a response via
# the API surface (GET .../ask_user/pending + POST .../ask_user/respond).
# The optional ``timeout_seconds`` falls back to the global yield cap
# when omitted. The optional ``response_schema`` is surfaced to the
# UI and validated server-side at POST time.
# ===========================================================================


class _AskUserArgs(BaseModel):
    """Prompt the operator sees and shape of the expected reply."""

    prompt: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description=(
            "Question or instruction shown to the operator. Newlines "
            "are preserved by the UI panel. Required."
        ),
    )
    response_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional JSON Schema the operator's response must satisfy. "
            "Validated server-side at POST time; a violation is "
            "surfaced inline in the UI without resuming the agent. "
            "Omit for free-text responses."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Optional per-call timeout. When omitted, falls back to "
            "the global yield cap (default 60 minutes). If the "
            "operator doesn't respond in time the resume hook returns "
            "``{timed_out: true, elapsed_seconds: ...}`` so the agent "
            "can decide whether to retry or proceed."
        ),
    )


def ask_user_resume(
    yield_metadata: dict[str, Any],
    event_payload: Any,
) -> ToolCallResult:
    """Resume hook for ask_user — translate payload into tool result.

    Three branches:

    * real response (``{"response": <any>}`` from the POST endpoint) →
      ``{"response": <any>}``
    * :class:`YieldTimeout` from the sweeper → ``{"timed_out": true,
      "elapsed_seconds": ...}``
    * :class:`YieldCancelled` from the cancel-yielded-tool API →
      ``{"cancelled": true, "reason": ..., "elapsed_seconds": ...}``

    ``yield_metadata`` carries ``parked_at_iso`` (worker-injected) so
    we can compute elapsed even if the event payload didn't include
    it (defensive — both timeout and cancel synthesise elapsed
    upstream via :func:`classify_resume_payload`, but the dataclass
    instance is the source of truth).
    """
    from primer.model.yield_ import YieldCancelled, YieldTimeout  # avoid cycle

    if isinstance(event_payload, YieldTimeout):
        return _ok(
            {
                "timed_out": True,
                "elapsed_seconds": event_payload.elapsed_seconds,
            }
        )
    if isinstance(event_payload, YieldCancelled):
        return _ok(
            {
                "cancelled": True,
                "reason": event_payload.reason,
                "elapsed_seconds": event_payload.elapsed_seconds,
            }
        )
    # Real operator response from the POST endpoint.
    response = (
        event_payload.get("response")
        if isinstance(event_payload, dict)
        else None
    )
    return _ok({"response": response})


async def _ask_user_handler(
    arguments: dict[str, Any],
    *,
    ctx: ToolContext,
) -> ToolCallResult | Yielded:
    try:
        args = _AskUserArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)

    # The event_key is keyed on (session_id, tool_call_id) so the API
    # endpoint can target a specific in-flight prompt. Without a
    # session id we'd lose disambiguation across concurrent sessions,
    # so fail loud.
    if ctx.session_id is None:
        return _err(
            "ask_user requires ctx.session_id; the worker must pass "
            "the live session id when invoking yielding tools",
            error_type="bad-request",
        )

    return Yielded(
        tool_name="",  # filled in by the provider
        event_key=f"ask_user:{ctx.session_id}:{ctx.tool_call_id}",
        timeout=args.timeout_seconds,
        resume_metadata={
            "prompt": args.prompt,
            "response_schema": args.response_schema,
            "tool_call_id": ctx.tool_call_id,
        },
    )


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
            ),
            _get_datetime_handler,
        ),
        "ask_user": (
            make_tool(
                id="ask_user",
                toolset_id=toolset_id,
                purpose=(
                    "Ask the human operator a question and pause the agent's "
                    "turn until they type a reply; returns ``{response: "
                    "<any>}`` (or ``{timed_out}`` / ``{cancelled}``)."
                ),
                when=(
                    "Use when you genuinely need human input (clarification, "
                    "approval, a choice the agent cannot make autonomously); "
                    "not for status updates, and not for waiting a fixed "
                    "duration (use ``sleep``)."
                ),
                args_schema=_AskUserArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={"prompt": "Proceed with deploy?"},
                        returns="operator's typed reply",
                        note="yielding; worker released",
                    ),
                ],
            ),
            _ask_user_handler,
        ),
        "sleep": (
            make_tool(
                id="sleep",
                toolset_id=toolset_id,
                purpose=(
                    "Pause this agent turn for ``seconds`` seconds "
                    "(fractional allowed); returns ``{requested_seconds, "
                    "elapsed_seconds}``."
                ),
                when=(
                    "Use when you must wait a fixed duration (polling with "
                    "backoff, deliberate pacing); not for waiting on a human "
                    "(use ``ask_user``)."
                ),
                args_schema=_SleepArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={"seconds": 5},
                        returns="resumes after 5s",
                        note="yielding; worker released",
                    ),
                ],
            ),
            _sleep_handler,
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
register_resume_hook("ask_user", ask_user_resume)


__all__ = ["MISC_TOOLSET_ID", "build_misc_toolset"]
