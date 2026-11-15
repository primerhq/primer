"""Recording relay: genuine upstream responses buffered before safe forwarding."""
from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
import httpx

from .contracts import BASE, MAX_BYTES, MAX_REQUESTS, MODEL, TOOLS, digest, inspect_effects, parse_response, system_head


RELAY_WALL_SECONDS = 35


class Relay:
    def __init__(self, upstream: str, *, transport=None):
        self.upstream = upstream
        self.transport = transport
        self.stage = "capability"
        self.packets: list[dict] = []
        self.failure: str | None = None
        self.unknown_upstream = False
        self.busy = False
        self.held = asyncio.Event()
        self.discard = asyncio.Event()
        self.arm_crash = False
        self.in_progress: set[asyncio.Task] = set()
        self.recovery_held = asyncio.Event()
        self.release_recovery = asyncio.Event()
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self.forward)

    async def forward(self, request: Request):
        raw = await request.body()
        if len(raw) > MAX_BYTES:
            self.failure = "request exceeds evidence bound"
            return Response(status_code=413)
        body = __import__('json').loads(raw)
        task = asyncio.create_task(self.exchange(body))
        self.in_progress.add(task)
        task.add_done_callback(self.in_progress.discard)

        async def stream():
            # Open HTTP immediately. Native SSE comments keep connect/stall
            # timers honest while genuine model bytes remain buffered for checks.
            while not task.done():
                yield b": waiting for genuine upstream completion\n\n"
                await asyncio.wait({task}, timeout=1)
            result = task.result()
            if result.status_code != 200:
                raise RuntimeError("F1 relay refused upstream response")
            yield result.body
        return StreamingResponse(stream(), media_type="text/event-stream")

    async def exchange(self, body: dict):
        started = time.monotonic()
        row = {"index": len(self.packets) + 1, "stage": self.stage, "upstream_complete": False,
               "disposition": "rejected", "request": {}, "elapsed_seconds": 0}
        acquired = False
        try:
            if self.failure or self.busy or len(self.packets) >= MAX_REQUESTS:
                raise ValueError("failed, concurrent or exhausted relay")
            self.busy = True
            acquired = True
            row["request"] = body
            self.packets.append(row)
            if body.get("model") != MODEL or body.get("stream") is not True:
                raise ValueError("unexpected model or nonstream request")
            expected_cap = 128 if self.stage == "capability" else 4096 if self.stage == "compact" else 1024
            if body.get("max_tokens") != expected_cap or body.get("n", 1) != 1:
                raise ValueError("missing/different output cap")
            if self.stage == "capability":
                if {x["function"]["name"] for x in body.get("tools", [])} != {"w4_probe_capability"}:
                    raise ValueError("unexpected capability surface")
            elif self.stage == "compact":
                if body.get("tools") or BASE in str(body["messages"]):
                    raise ValueError("compactor contains launch base or tools")
            elif self.stage in {"cycle1", "cycle2", "recovery"}:
                if not system_head(body).startswith(BASE + "\n\n") or {x["function"]["name"] for x in body["tools"]} != TOOLS:
                    raise ValueError("changed launch base/tool surface")
            else:
                raise ValueError("unknown relay stage")
            timeout = httpx.Timeout(20, connect=5, write=5, pool=5)
            async with asyncio.timeout(RELAY_WALL_SECONDS):
                async with httpx.AsyncClient(timeout=timeout, transport=self.transport, trust_env=False) as client:
                    self.unknown_upstream = True
                    async with client.stream("POST", self.upstream + "/chat/completions", json=body) as upstream:
                        upstream.raise_for_status()
                        raw = bytearray()
                        async for chunk in upstream.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_BYTES:
                                raise ValueError("upstream response exceeds bound")
                    data = bytes(raw)
                    parsed = parse_response(data)
                    row.update(response_hex=data.hex(), response_sha256=digest(data), upstream_complete=True)
                    self.unknown_upstream = False
                    if self.stage == "capability":
                        if len(parsed["calls"]) != 1 or parsed["calls"][0]["name"] != "w4_probe_capability" or parsed["calls"][0]["arguments"] != {"ok": True}:
                            raise ValueError("model did not produce genuine capability call")
                    elif self.stage != "compact":
                        inspect_effects(parsed)
                    if self.arm_crash and self.stage == "cycle2" and [c["name"] for c in parsed["calls"]] == ["workspace__read"]:
                        self.arm_crash = False
                        self.held.set()
                        await asyncio.wait_for(self.discard.wait(), timeout=5)
                        row["disposition"] = "discarded_at_crash"
                        return Response(status_code=502)
                    if self.stage == "recovery" and not self.release_recovery.is_set():
                        # Keep an actual new claim observable before the fast model completes.
                        self.recovery_held.set()
                        await asyncio.wait_for(self.release_recovery.wait(), timeout=5)
                    row["disposition"] = "forwarded"
                    return Response(data, media_type="text/event-stream")
        except Exception as exc:
            self.failure = type(exc).__name__ + ": " + str(exc)
            row["failure"] = self.failure
            return Response(status_code=502)
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - started, 6)
            if acquired:
                self.busy = False
