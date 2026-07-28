"""The host side of the shim's wire format.

One JSON round trip: the host writes a request on stdin, the shim writes
exactly one response object on stdout. Anything else the process emits is
treated as a failure, never as a value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PHASE_CALL = "call"
PHASE_RESUME = "resume"


@dataclass
class ShimResponse:
    ok: bool
    value: Any = None
    yield_request: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def build_request(
    *,
    module: str,
    fn: str,
    phase: str,
    args: dict[str, Any] | None,
    ctx: dict[str, Any],
    payload: Any = None,
    meta: Any = None,
    cpu_seconds: int,
    address_space_bytes: int,
) -> dict[str, Any]:
    """Build the request object the shim reads from stdin."""
    return {
        "module": module,
        "fn": fn,
        "phase": phase,
        "args": args or {},
        "ctx": ctx,
        "payload": payload,
        "meta": meta,
        "limits": {
            "cpu_seconds": cpu_seconds,
            "address_space_bytes": address_space_bytes,
        },
    }


def parse_response(raw: str) -> ShimResponse:
    """Parse the shim's stdout.

    Malformed output is an error result, never a value. A tool whose runner
    crashed half way through must not be able to look like a tool that
    returned successfully.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ShimResponse(
            ok=False,
            error={
                "type": "ProtocolError",
                "message": "the runner produced output that is not JSON",
                "traceback": raw[:2000],
            },
        )
    if not isinstance(data, dict) or "ok" not in data:
        return ShimResponse(
            ok=False,
            error={
                "type": "ProtocolError",
                "message": "malformed runner response",
                "traceback": raw[:2000],
            },
        )
    return ShimResponse(
        ok=bool(data["ok"]),
        value=data.get("value"),
        yield_request=data.get("yield"),
        error=data.get("error"),
    )
