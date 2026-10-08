"""The system ``create_toolset`` / ``update_toolset`` tools: a python toolset is admin-only, and a caller below admin cannot repoint a
toolset at a new endpoint while its stored secrets ride along (security sweep AUTHZ-01, SSRF-01, SSRF-02, SEC-02; the tool half of
``tests/api/test_toolset_write_privilege.py``).

A python toolset's source runs on the server host (LocalHardenedRunner), so creating or changing one is the same class of power as a
stdio MCP toolset: either side of an update counts, as for stdio (``toolset_admin_reason``). A secret the caller sends back masked is
restored from the stored row (``preserve_masked_secrets``); when the same update changes the URL or the OAuth endpoints, that would
hand the stored headers / client secret to a server of the caller's choosing, so a caller below admin must re-enter the secrets.
"""

from __future__ import annotations

import json

import pytest

from primer.api.registries import ProviderRegistry
from primer.model.provider import Toolset
from primer.model.yield_ import ToolContext
from primer.toolset.system import build_system_toolset
from tests._support.caller import caller as _ctx
from tests.conftest import _FakeStorageProvider

PY_SOURCE = '''
@primer_tool()
def greet(name: str) -> str:
    """Greet a person by name.

    Use when you need a friendly greeting.

    Args:
        name: Who to greet.
    """
    return f"hello {name}"
'''
PYTHON = {"id": "ts-py", "provider": "python", "config": {"source": PY_SOURCE, "source_version": 1}}
PYTHON_OTHER = {**PYTHON, "config": {"source": PY_SOURCE.replace("hello", "hi"), "source_version": 1}}
HTTP_PLAIN = {"id": "ts-py", "provider": "mcp", "config": {"transport": "http", "config": {"url": "http://127.0.0.1:9/mcp"}}}

MASK = "**********"
SECRET = "Bearer s3cret-token"
CLIENT_SECRET = "client-s3cret"


def _http(url: str, header: str, client_secret: str | None = None, toolset_id: str = "ts-http") -> dict:
    config: dict = {"url": url, "headers": {"Authorization": header}}
    if client_secret is not None:
        config = {
            "url": url,
            "oauth": {"redirect_uri": "http://127.0.0.1:9/cb", "static_client": {"client_id": "c", "client_secret": client_secret}},
        }
    return {"id": toolset_id, "provider": "mcp", "config": {"transport": "http", "config": config}}


@pytest.fixture
def toolset_and_storage():
    sp = _FakeStorageProvider()
    reg = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda p: object(),
    )
    return build_system_toolset(storage_provider=sp, provider_registry=reg), sp.get_storage(Toolset)


async def _call(toolset, tool: str, arguments: dict, ctx: ToolContext | None):
    return await toolset.call(tool_name=tool, arguments=arguments, principal=None, ctx=ctx)


def _error_type(result) -> str | None:
    return json.loads(result.output).get("type") if result.is_error else None


# ---- python toolsets (AUTHZ-01, SSRF-01) ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("ctx", [_ctx("user"), None], ids=["user", "no-identity"])
async def test_a_caller_below_admin_cannot_create_a_python_toolset(toolset_and_storage, ctx):
    toolset, storage = toolset_and_storage

    result = await _call(toolset, "create_toolset", {"entity": PYTHON}, ctx)

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert "python" in json.loads(result.output)["message"], result.output
    assert await storage.get("ts-py") is None, "the refused toolset was stored"


async def test_a_user_run_cannot_change_a_python_toolset(toolset_and_storage):
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": PYTHON}, _ctx("admin"))).is_error

    result = await _call(toolset, "update_toolset", {"id": "ts-py", "entity": PYTHON_OTHER}, _ctx("user"))

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert "hello" in (await storage.get("ts-py")).config.source


async def test_a_user_run_cannot_turn_an_http_toolset_into_a_python_one(toolset_and_storage):
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": {**HTTP_PLAIN, "id": "ts-x"}}, _ctx("user"))).is_error

    result = await _call(toolset, "update_toolset", {"id": "ts-x", "entity": {**PYTHON, "id": "ts-x"}}, _ctx("user"))

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert (await storage.get("ts-x")).provider.value == "mcp"


async def test_a_user_run_cannot_turn_a_python_toolset_into_an_http_one(toolset_and_storage):
    """The stored row counts too: the incoming body runs nothing, so only the ``existing`` side of the rule can refuse it."""
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": PYTHON}, _ctx("admin"))).is_error

    result = await _call(toolset, "update_toolset", {"id": "ts-py", "entity": HTTP_PLAIN}, _ctx("user"))

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert (await storage.get("ts-py")).provider.value == "python"


async def test_an_admin_can_create_and_change_a_python_toolset(toolset_and_storage):
    toolset, storage = toolset_and_storage

    created = await _call(toolset, "create_toolset", {"entity": PYTHON}, _ctx("admin"))
    changed = await _call(toolset, "update_toolset", {"id": "ts-py", "entity": PYTHON_OTHER}, _ctx("admin"))

    assert not created.is_error and not changed.is_error, (created.output, changed.output)
    assert "hi" in (await storage.get("ts-py")).config.source


# ---- repointing a toolset that holds secrets (SSRF-02, SEC-02) ------------------------------------------------------------------


async def test_a_user_run_cannot_repoint_a_toolset_while_its_headers_ride_along_masked(toolset_and_storage):
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": _http("http://127.0.0.1:9/mcp", SECRET)}, _ctx("admin"))).is_error

    result = await _call(
        toolset, "update_toolset", {"id": "ts-http", "entity": _http("http://evil.example/mcp", MASK)}, _ctx("user"),
    )

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert "re-enter" in json.loads(result.output)["message"], result.output
    stored = (await storage.get("ts-http")).config.config
    assert stored.url == "http://127.0.0.1:9/mcp"
    assert stored.headers["Authorization"].get_secret_value() == SECRET


async def test_a_user_run_cannot_repoint_a_toolset_while_its_oauth_client_secret_rides_along(toolset_and_storage):
    toolset, storage = toolset_and_storage
    body = _http("http://127.0.0.1:9/mcp", SECRET, client_secret=CLIENT_SECRET)
    assert not (await _call(toolset, "create_toolset", {"entity": body}, _ctx("admin"))).is_error

    result = await _call(
        toolset, "update_toolset",
        {"id": "ts-http", "entity": _http("http://evil.example/mcp", SECRET, client_secret=MASK)}, _ctx("user"),
    )

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert (await storage.get("ts-http")).config.config.url == "http://127.0.0.1:9/mcp"


@pytest.mark.parametrize(
    "oauth_change",
    [{"redirect_uri": "http://evil.example/cb"}, {"resource_uri": "http://evil.example/mcp"}],
    ids=["redirect_uri", "resource_uri"],
)
async def test_a_user_run_cannot_move_only_the_oauth_endpoints_while_the_client_secret_rides_along(toolset_and_storage, oauth_change):
    """The URL stays put: only the OAuth endpoints count as the move, so the rule must compare them too."""
    toolset, storage = toolset_and_storage
    assert not (await _call(
        toolset, "create_toolset", {"entity": _http("http://127.0.0.1:9/mcp", SECRET, client_secret=CLIENT_SECRET)}, _ctx("admin"),
    )).is_error
    body = _http("http://127.0.0.1:9/mcp", SECRET, client_secret=MASK)
    body["config"]["config"]["oauth"].update(oauth_change)

    result = await _call(toolset, "update_toolset", {"id": "ts-http", "entity": body}, _ctx("user"))

    assert result.is_error and _error_type(result) == "forbidden", result.output
    oauth = (await storage.get("ts-http")).config.config.oauth
    assert (str(oauth.redirect_uri), oauth.resource_uri) == ("http://127.0.0.1:9/cb", None)


async def test_a_user_run_may_repoint_a_toolset_when_it_re_enters_the_secrets(toolset_and_storage):
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": _http("http://127.0.0.1:9/mcp", SECRET)}, _ctx("admin"))).is_error

    result = await _call(
        toolset, "update_toolset", {"id": "ts-http", "entity": _http("http://other.example/mcp", "Bearer new")}, _ctx("user"),
    )

    assert not result.is_error, result.output
    stored = (await storage.get("ts-http")).config.config
    assert (stored.url, stored.headers["Authorization"].get_secret_value()) == ("http://other.example/mcp", "Bearer new")


async def test_a_user_run_keeps_masked_secrets_when_the_endpoint_does_not_change(toolset_and_storage):
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": _http("http://127.0.0.1:9/mcp", SECRET)}, _ctx("admin"))).is_error

    result = await _call(toolset, "update_toolset", {"id": "ts-http", "entity": _http("http://127.0.0.1:9/mcp", MASK)}, _ctx("user"))

    assert not result.is_error, result.output
    assert (await storage.get("ts-http")).config.config.headers["Authorization"].get_secret_value() == SECRET


async def test_an_admin_may_repoint_a_toolset_and_keep_its_masked_secrets(toolset_and_storage):
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": _http("http://127.0.0.1:9/mcp", SECRET)}, _ctx("admin"))).is_error

    result = await _call(
        toolset, "update_toolset", {"id": "ts-http", "entity": _http("http://other.example/mcp", MASK)}, _ctx("admin"),
    )

    assert not result.is_error, result.output
    stored = (await storage.get("ts-http")).config.config
    assert (stored.url, stored.headers["Authorization"].get_secret_value()) == ("http://other.example/mcp", SECRET)


# ---- an admin converting an MCP toolset into a python one ----------------------------------------------------------------------


async def test_an_admin_run_can_convert_an_mcp_toolset_into_a_python_one_and_it_starts_at_the_first_version(toolset_and_storage):
    """The update raised (``'McpConfig' object has no attribute 'source_version'``): with no prior python version to bump, the
    converted toolset starts at 1 whatever the caller sends."""
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": {**HTTP_PLAIN, "id": "ts-x"}}, _ctx("admin"))).is_error
    body = {**PYTHON, "id": "ts-x", "config": {"source": PY_SOURCE, "source_version": 7}}

    result = await _call(toolset, "update_toolset", {"id": "ts-x", "entity": body}, _ctx("admin"))

    assert not result.is_error, result.output
    stored = await storage.get("ts-x")
    assert (stored.provider.value, stored.config.source_version) == ("python", 1)
