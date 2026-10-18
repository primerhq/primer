"""The draft tests of embedding and cross-encoder providers and the Hugging Face Hub check against a live server (revamp plan 02, tasks 3 and 4).

The live server has no Hugging Face Hub it may reach (and no test may reach the real one), so the Hub arm is held here to what needs no network: a name that is not a model
id is answered without a request. The OpenAI-compatible arm runs against a stub on localhost, the way the speech journey does. What each answer of the Hub means is pinned with
``respx`` in ``tests/common/test_hf_hub.py``, ``tests/api/test_hf_hub_route.py`` and ``tests/api/test_model_provider_draft_tests.py``, which drive the real app.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest
import uvicorn
from fastapi import FastAPI

PROBE_TOOLS = {"check_huggingface_models", "test_embedding_provider", "test_cross_encoder_provider"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/models")
    async def models() -> dict:
        return {"data": [{"id": "stub-embed"}, {"id": "stub-chat"}]}

    return app


@pytest.fixture
async def stub_server():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(_stub_app(), host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_an_openai_embedding_draft_lists_the_models_of_a_stub_server(client, api_prefix, stub_server, unique_suffix) -> None:
    draft_id = f"e2e-draft-{unique_suffix}"

    r = await client.post(
        f"{api_prefix}/embedding_providers/_test",
        json={"id": draft_id, "provider": "openai", "config": {"url": stub_server}},
    )

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "models": ["stub-embed", "stub-chat"]}
    assert (await client.get(f"{api_prefix}/embedding_providers/{draft_id}")).status_code == 404, "a test stores nothing"


@pytest.mark.asyncio
async def test_a_draft_that_is_not_a_row_is_an_answer_on_both_classes_and_never_a_405(client, api_prefix) -> None:
    for plural in ("embedding_providers", "cross_encoder_providers"):
        r = await client.post(f"{api_prefix}/{plural}/_test", json={})

        assert r.status_code == 200, (plural, r.text)
        assert r.json()["ok"] is False and r.json()["error"].startswith("invalid draft"), (plural, r.text)


@pytest.mark.asyncio
async def test_the_hub_check_answers_a_name_that_is_not_a_model_id_without_leaving_the_server(client, api_prefix) -> None:
    r = await client.post(f"{api_prefix}/huggingface/_check_models", json={"models": ["../../api/whoami", "a/b/c"]})

    assert r.status_code == 200, r.text
    assert [(m["name"], m["ok"], m["status"]) for m in r.json()["models"]] == [
        ("../../api/whoami", False, "invalid_name"),
        ("a/b/c", False, "invalid_name"),
    ]


@pytest.mark.asyncio
async def test_a_hub_check_of_nothing_or_of_too_many_is_a_422(client, api_prefix) -> None:
    empty = await client.post(f"{api_prefix}/huggingface/_check_models", json={"models": []})
    many = await client.post(f"{api_prefix}/huggingface/_check_models", json={"models": [f"o/m{i}" for i in range(51)]})

    assert empty.status_code == 422 and many.status_code == 422


@pytest.mark.asyncio
async def test_an_anonymous_caller_is_refused_by_all_three_routes(anon_client, api_prefix) -> None:
    bodies = {
        "huggingface/_check_models": {"models": ["BAAI/bge-small-en-v1.5"]},
        "embedding_providers/_test": {},
        "cross_encoder_providers/_test": {},
    }
    for path, body in bodies.items():
        r = await anon_client.post(f"{api_prefix}/{path}", json=body)

        assert r.status_code == 401, (path, r.text)


@pytest.mark.asyncio
async def test_the_system_toolset_lists_the_probe_tools(client, api_prefix) -> None:
    r = await client.get(f"{api_prefix}/toolsets/system/tools")

    assert r.status_code == 200, r.text
    assert PROBE_TOOLS <= {tool["id"].removeprefix("system__") for tool in r.json()["tools"]}
