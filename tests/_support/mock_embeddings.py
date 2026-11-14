"""A deterministic local OpenAI embedding wire fixture, started only explicitly.

It exercises the production adapter and vector store in CI without downloads or
external credentials. Its vectors depend on input text; it is not a semantic
quality evaluation. Unit tests can drive build_app through ASGI with no socket.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import socket
import threading
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn


@dataclass
class EmbeddingScript:
    model: str = "w7-embedding"
    requests: list[dict] = field(default_factory=list)

    @staticmethod
    def vector(text: str) -> list[float]:
        lower = text.casefold()
        return [float("cedar" in lower), float("quartz" in lower), 0.1]

    def response(self, body: dict) -> dict:
        assert body["model"] == self.model, body
        inputs = body["input"]
        texts = [inputs] if isinstance(inputs, str) else inputs
        assert isinstance(texts, list) and texts and all(isinstance(t, str) for t in texts), body
        self.requests.append(body)
        return {
            "object": "list", "model": self.model,
            "data": [{"object": "embedding", "index": i, "embedding": self.vector(text)} for i, text in enumerate(texts)],
            "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
        }


def build_app(script: EmbeddingScript) -> Starlette:
    async def embeddings(request: Request):
        return JSONResponse(script.response(await request.json()))

    return Starlette(routes=[Route("/v1/embeddings", embeddings, methods=["POST"])])


@contextmanager
def embedding_server(script: EmbeddingScript):
    # Hold the bound socket through uvicorn startup, avoiding free-port races.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(script), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    try:
        thread.start()
        for _ in range(100):
            if server.started:
                break
            assert thread.is_alive(), "embedding fixture exited during startup"
            time.sleep(0.05)
        else:
            raise RuntimeError("embedding fixture did not start")
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "embedding fixture did not stop"
