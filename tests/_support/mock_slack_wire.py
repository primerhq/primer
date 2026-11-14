"""Private Slack Web API wire for CI; importing this opens no socket.

The SDK and production adapter run in the isolated child. Socket Mode delivery
is injected at the production raw-event boundary; no real Slack credentials,
gateway connection or shared provider configuration are used.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import socket
import threading
import time
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn


@dataclass
class SlackWire:
    requests: list[dict] = field(default_factory=list)
    counter: int = 0

    def respond(self, method: str, payload: dict) -> dict:
        # Record protocol data, never credentials/Authorization headers.
        self.requests.append({"method": method, "payload": payload})
        if method == "auth.test":
            return {"ok": True, "user_id": "UBOT", "bot_id": "BBOT", "team_id": "TLOCAL"}
        if method == "conversations.info":
            return {"ok": True, "channel": {"id": payload["channel"], "is_member": True}}
        if method == "chat.postMessage":
            assert payload.get("channel") and isinstance(payload.get("text"), str)
            self.counter += 1
            return {"ok": True, "channel": payload["channel"],
                    "ts": f"1731600000.{self.counter:06d}", "message": payload}
        return {"ok": False, "error": "fixture_unknown_method"}


def build_app(wire: SlackWire) -> Starlette:
    async def api(request: Request):
        raw = await request.body()
        if request.headers.get("content-type", "").startswith("application/json"):
            payload = json.loads(raw)
        else:
            payload = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            payload.update(request.query_params)
        return JSONResponse(wire.respond(request.path_params["method"], payload))

    return Starlette(routes=[Route("/api/{method}", api, methods=["GET", "POST"])])


@contextmanager
def slack_server(wire: SlackWire):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(wire), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    try:
        thread.start()
        for _ in range(100):
            if server.started:
                break
            assert thread.is_alive(), "Slack fixture exited during startup"
            time.sleep(.05)
        else:
            raise RuntimeError("Slack fixture did not start")
        yield f"http://127.0.0.1:{port}/api/"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "Slack fixture did not stop"
