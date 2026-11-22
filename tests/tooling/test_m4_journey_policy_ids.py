"""The M4 e2e journey's constants must match the live surfaces they drive (the journey is CI-only, so the unit lane pins them).

* The policy ids are the canonical seeded ids, never hand-rolled: a drifted spelling 404s against the shipped row.
* The MCP client timeout is an ``httpx2.Timeout``: ``create_mcp_http_client`` builds an ``httpx2.AsyncClient``, and an
  ``httpx.Timeout`` is a different package's class that httpcore2 cannot compose (job 131972235488 TypeError).
* The synchronous ``_call_output`` helper is never itself awaited: a double await raises TypeError only in the live journey
  (job 131991208076), so the unit lane scans the journey source for it.
* Readiness requires a successful semantic query using the same seeded model as bootstrap.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import httpx2
import pytest

import tests.e2e.test_tools_universal_m4_mcp_journey as m4_journey
from primer.bootstrap.seed import default_policy_id
from tests.e2e.test_tools_universal_m4_mcp_journey import _MCP_TIMEOUT, _POLICY_IDS

_TOOLS = ("put_config", "delete_config", "bootstrap")


def test_m4_journey_policy_ids_are_the_canonical_seeded_ids() -> None:
    assert set(_POLICY_IDS) == set(_TOOLS), _POLICY_IDS
    for tool in _TOOLS:
        assert _POLICY_IDS[tool] == default_policy_id("internal_collections", tool)


def test_m4_journey_mcp_timeout_is_httpx2() -> None:
    # The SDK read bound is independent of the generated route's dispatch deadline.
    assert isinstance(_MCP_TIMEOUT, httpx2.Timeout), type(_MCP_TIMEOUT)
    assert _MCP_TIMEOUT.read == 120.0


@pytest.mark.parametrize("embed_status", [200, 502])
async def test_m4_preparation_requires_a_successful_semantic_query_before_bootstrap(embed_status):
    path = "/v1/collections/m4-warmup-test"
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.method == "PUT":
            assert json.loads(request.content) == {
                "embedder": {"provider_id": "huggingface", "model": "BAAI/bge-small-en-v1.5"},
                "vector_store_provider_id": "lance",
            }
            return httpx.Response(200, json={"search": {"state": "ready"}})
        assert json.loads(request.content)["query"].strip()
        return httpx.Response(embed_status, json={"hits": []} if embed_status == 200 else {"detail": "embedder unavailable"})

    async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        if embed_status == 200:
            await m4_journey._prepare_reserved_embedder(client, path)
        else:
            with pytest.raises(AssertionError, match="embedder unavailable"):
                await m4_journey._prepare_reserved_embedder(client, path)
    assert calls == [("PUT", f"{path}/search"), ("POST", f"{path}/search")]


def test_m4_journey_never_awaits_the_sync_call_output_helper() -> None:
    # ``_call_output`` is synchronous; awaiting it (``await _call_output(...)``) raises TypeError only
    # in the live CI journey - a double await cost a 27-minute live run (job 131991208076). The unit
    # lane cannot run the journey, so scan its source: no Await may wrap a direct _call_output call.
    tree = ast.parse(Path(m4_journey.__file__).read_text())
    double_awaits = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_call_output"
    ]
    assert not double_awaits, double_awaits
