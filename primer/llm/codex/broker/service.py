"""Credential-owning broker core and Linux peer-checked, bounded IPC service.

Host code provisions a separate broker identity and listening Unix socket.
No executable, config path, native effect or credential crosses this protocol.
"""
from __future__ import annotations

import asyncio
import socket
import struct
import time
from collections.abc import AsyncIterator

from primer.llm.codex.boundary import BoundaryError, BridgeConfig
from primer.llm.codex.bridge import ModelOnlyBridge, _validate_request
from primer.llm.codex.lifecycle import drain
from primer.llm.codex.request import ModelRequest, compact, decode
from .auth import SignIn
from .delivery import DeliveryReceipts, step_digest
from .http import MODELS, FixedHTTP
from .security import require, text


class _Channel:
    def __init__(self, http: FixedHTTP, wire: dict, token: str):
        self._events = http.events(wire, token)

    def __aiter__(self):
        return self._events

    async def aclose(self):
        await self._events.aclose()


class Broker:
    def __init__(self, auth: SignIn, http: FixedHTTP):
        self._auth, self._http = auth, http
        self._closed = False
        self._bridges = set()
        self.config = BridgeConfig(max_active_steps=1)
        self._delivery = DeliveryReceipts()

    async def models(self, account: str) -> list[dict]:
        require(not self._closed)
        token, generation = await self._auth.credential(account)
        policy = self._auth.snapshot()
        require(policy.account == account)
        result = await self._http.json(MODELS, bearer=token)
        require(not self._closed and self._auth.snapshot() == policy and policy.expires_at > time.time())
        require(type(result.get("models")) is list and len(result["models"]) <= 4096)
        models, seen = [], set()
        for item in result["models"]:
            require(type(item) is dict)
            if item.get("visibility") != "list":
                continue
            slug = text(item.get("slug"), 256)
            require(slug not in seen)
            seen.add(slug)
            if slug in policy.models:
                models.append({"slug": slug, "display_name": text(item.get("display_name"), 256)})
        return models

    def authorize_delivery(self, account: str, receipt: str, request: str, event: str) -> dict[str, float]:
        # snapshot checks live registration, disposal fences, signed policy,
        # credential generation and expiry. No refresh/model network I/O.
        require(not self._closed)
        return self._delivery.authorize(receipt, account=account, request=request,
                                        event=event, current=self._auth.snapshot())

    async def step(self, account: str, request: ModelRequest, *, delivery_nonce: str | None = None) -> AsyncIterator[dict]:
        deadline = time.monotonic() + self.config.deadline_seconds
        wire = _validate_request(request, self.config)  # Before network or construction.
        request_id = step_digest(request, delivery_nonce) if delivery_nonce is not None else None
        require(not self._closed)
        models = await self.models(account)
        require(wire["model"] in {m["slug"] for m in models})
        token, generation = await self._auth.credential(account)
        expected = self._auth.snapshot()
        expected.admit(account, wire["model"])

        class Authority:
            async def current(_):
                current = self._auth.snapshot()
                require(current == expected)
                return current

        bridge = ModelOnlyBridge(authority=Authority(), account=account, config=self.config,
                                 transport_factory=lambda body: _Channel(self._http, body, token))
        require(not self._closed)
        self._bridges.add(bridge)
        stream = bridge.stream(request)
        try:
            async for event in stream:
                data = event.model_dump(mode="json")
                if request_id is None:
                    yield data
                elif event.type in {"text_delta", "reasoning_delta"}:
                    yield {"event": data}
                else:
                    yield {"event": data, "delivery": self._delivery.seal(
                        account=account, model=wire["model"], policy=expected,
                        request=request_id, event=data, deadline=deadline,
                    )}
        finally:
            try:
                await bridge.aclose()
            finally:
                self._bridges.discard(bridge)

    async def close(self):
        self._closed = True
        results = await asyncio.gather(*(b.aclose() for b in tuple(self._bridges)), return_exceptions=True)
        await self._http.close()
        if any(isinstance(result, BaseException) for result in results):
            raise BoundaryError("Broker shutdown encountered a cleanup failure")


class BrokerServer:
    """One operation per connection; real SO_PEERCRED, not claimed uid.

    Account is pinned by trusted host composition, not selected in messages.
    At most eight admitted operations; slow peers have absolute deadlines.
    Management/auth methods are absent. No policy/transport/config override.
    """
    def __init__(self, broker: Broker, *, peer_uid: int, account: str):
        require(type(peer_uid) is int and peer_uid >= 0)
        self._broker, self._uid, self._account = broker, peer_uid, text(account)
        self._active = set()
        self._closed = False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        admitted = False
        try:
            sock = writer.get_extra_info("socket")
            require(sock is not None and sock.family == socket.AF_UNIX)
            _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            require(uid == self._uid and not self._closed and len(self._active) < 8)
            self._active.add(task)
            admitted = True
            async with asyncio.timeout(300):
                async with asyncio.timeout(5):
                    raw = await reader.readline()
                require(len(raw) <= 1048576 and raw.endswith(b"\n"))
                message = decode(raw)
                require(type(message) is dict and set(message) == {"method", "params"}
                        and type(message["params"]) is dict)
                method, params = message["method"], message["params"]
                require((method == "models/list" and not params) or
                        (method == "step/start" and set(params) == {"request", "aliases", "delivery_nonce"}) or
                        (method == "step/authorize" and set(params) == {"receipt", "request", "event"}))

                async def emit(value):
                    encoded = compact(value).encode() + b"\n"
                    require(len(encoded) <= 1048576)
                    writer.write(encoded)
                    await writer.drain()

                async def operation():
                    if method == "models/list":
                        await emit({"models": await self._broker.models(self._account)})
                    elif method == "step/authorize":
                        validity = self._broker.authorize_delivery(self._account, params["receipt"],
                                                                   params["request"], params["event"])
                        await emit({"authorized": True, **validity})
                    else:
                        require(type(params["aliases"]) is list and all(type(p) is list and len(p) == 2 for p in params["aliases"]))
                        request = ModelRequest(compact(params["request"]), tuple(tuple(p) for p in params["aliases"]))
                        # Validate before dispatch; null cannot request the
                        # trusted in-process compatibility path without receipts.
                        step_digest(request, params["delivery_nonce"])
                        events = self._broker.step(self._account, request, delivery_nonce=params["delivery_nonce"])
                        try:
                            async for event in events:
                                await emit(event)
                        finally:
                            await events.aclose()

                work = asyncio.create_task(operation())
                disconnect = asyncio.create_task(reader.read(1))
                try:
                    done, _ = await asyncio.wait({work, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                    if disconnect in done:
                        raise BoundaryError("Broker peer disconnected or pipelined input")
                    await work
                finally:
                    async def cleanup():
                        disconnect.cancel()
                        work.cancel()
                        await asyncio.gather(disconnect, return_exceptions=True)
                        await drain(work)
                    await drain(asyncio.create_task(cleanup()))
        except asyncio.CancelledError:
            raise
        except Exception:
            # No upstream error body, token, auth URL, request, or traceback on IPC.
            if admitted and not writer.is_closing():
                writer.write(b'{"error":"broker_refused"}\n')
                try:
                    async with asyncio.timeout(1):
                        await writer.drain()
                except Exception:
                    pass
        finally:
            self._active.discard(task)
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except Exception:
                pass

    async def serve(self, sock: socket.socket):
        require(sock.family == socket.AF_UNIX and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1)
        return await asyncio.start_unix_server(self.handle, sock=sock, limit=1048576)

    async def close(self):
        self._closed = True
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._broker.close()
