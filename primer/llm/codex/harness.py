"""Executable OFFLINE protocol harness; never authenticates or opens a network.

Usage: python -m primer.llm.codex.harness --transcript synthetic.json
A transcript is synthetic Responses events, not an upstream safety receipt.
Stdin accepts only bridge/status and step/start with a complete replay request.
Every request gets a fresh bridge/channel. Unknown RPCs/config are refused
before a channel can be constructed. EOF closes the process with no children.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from primer.llm.codex.boundary import BoundaryError, BridgeConfig, PolicySnapshot
from primer.llm.codex.bridge import ModelOnlyBridge
from primer.llm.codex.request import ModelRequest, compact, decode


class RecordedChannel:
    """Finite replay transport. No code execution, URLs, imports or callbacks."""
    def __init__(self, frames: list[dict]):
        self._frames = iter(frames)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            frame = next(self._frames)
        except StopIteration:
            raise StopAsyncIteration from None
        await asyncio.sleep(0)
        return frame

    async def aclose(self):
        self.closed = True


class RecordedPolicy:
    def __init__(self, model: str):
        self.snapshot = PolicySnapshot("offline", "synthetic-v1", time.time() + 60, frozenset({model}))

    async def current(self):
        return self.snapshot


async def dispatch(message: dict, frames: list[dict]) -> dict:
    """No raw upstream RPC forwarding. Recording mode is explicit in output."""
    ident = message.get("id")
    if type(ident) not in {str, int}:
        raise BoundaryError("Invalid harness request ID")
    if set(message) != {"id", "method", "params"} or type(message["params"]) is not dict:
        raise BoundaryError("Invalid harness envelope")
    method, params = message["method"], message["params"]
    if method == "bridge/status" and not params:
        return {"id": ident, "result": {"mode": "offline", "live_enabled": False}}
    if method != "step/start" or set(params) != {"request", "aliases", "config"}:
        raise BoundaryError("Unsupported harness RPC")
    config = BridgeConfig.from_mapping(params["config"])
    request = ModelRequest(compact(params["request"]), tuple(tuple(pair) for pair in params["aliases"]))
    bridge = ModelOnlyBridge(authority=RecordedPolicy(params["request"]["model"]), account="offline",
                             transport_factory=lambda _: RecordedChannel(frames), config=config)
    try:
        result = [event.model_dump(mode="json") async for event in bridge.stream(request)]
        return {"id": ident, "result": {"mode": "offline", "events": result}}
    finally:
        await bridge.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", required=True, type=Path)
    args = parser.parse_args()
    # Bounded local data read only; file content is never evaluated as Python.
    with args.transcript.open("rb") as stream:
        raw = stream.read(8_388_609)
    if len(raw) > 8_388_608:
        raise SystemExit("Transcript too large")
    frames = decode(raw)
    if type(frames) is not list:
        raise SystemExit("Transcript must be an event list")
    while raw := sys.stdin.buffer.readline(1_048_578):
        try:
            if len(raw) > 1_048_576 or not raw.endswith(b"\n"):
                raise BoundaryError("Invalid harness frame size")
            message = decode(raw)
            answer = asyncio.run(dispatch(message, frames))
        except (BoundaryError, ValueError, TypeError, KeyError, AttributeError):
            # Never echo malformed requests, model text, policy or credentials.
            answer = {"error": {"code": "codex_boundary_refused", "message": "Offline request refused"}}
        print(compact(answer), flush=True)


if __name__ == "__main__":
    main()
