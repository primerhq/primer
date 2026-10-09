"""The system tools keep a masked secret only for the origin it was stored for, refuse a served mask on create, and answer a refusal (ticket 01a1212a).

``update_llm_provider`` and the other ``update_*`` tools run ``preserve_masked_secrets``: a secret the matching ``get_*`` served masked and the caller sent back unchanged keeps its stored value. Part A
of the ticket binds that to the config's origin (scheme, host, port): an update that moves ``url`` to another host and leaves the ``api_key`` mask alone is refused with ``type=validation-error``
and stores nothing (an agent run an admin started can call these tools; this is the shape a prompt injection takes). Part B: ``create_*`` refuses a body that carries a served mask (the copy-a-provider
move: ``get_llm_provider`` then ``create_llm_provider`` under a new id), ``update_toolset`` answers a refusal instead of raising it out of the ``admin_when`` check, the refusal is
``type=validation-error`` (pinned), and the ``update_*`` descriptor tells the agent so.
"""

from __future__ import annotations

import json

import pytest

from primer.model.provider import EmbeddingProvider, LLMProvider
from tests._support.caller import ADMIN_CALLER, caller
from tests.toolset.test_system import pr, sp, system_toolset  # noqa: F401  (fixtures)

KEY = "sk-live-0123456789abcdef"
HOME = "https://home.example/v1"
AWAY = "https://attacker.example/v1"


def _llm(url: str = HOME, row_id: str = "llm-o") -> dict:
    return {
        "id": row_id, "provider": "openchat", "models": [{"name": "m", "context_length": 8192}],
        "config": {"url": url, "api_key": KEY, "flavor": "other"}, "limits": {"max_concurrency": 1},
    }


async def _create(toolset, body: dict, kind: str = "llm_provider"):
    result = await toolset.call(tool_name=f"create_{kind}", arguments={"entity": body}, ctx=ADMIN_CALLER)
    assert not result.is_error, result.output
    return result


async def _served(toolset, row_id: str = "llm-o", kind: str = "llm_provider") -> dict:
    return json.loads((await toolset.call(tool_name=f"get_{kind}", arguments={"id": row_id})).output)


def _error_type(result) -> str:
    return json.loads(result.output)["type"]


# ---- part A: the api_key and the origin -------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_update_that_moves_the_host_and_keeps_the_key_mask_is_refused(system_toolset, sp) -> None:
    await _create(system_toolset, _llm())
    served = await _served(system_toolset)
    assert served["config"]["api_key"].startswith("**********") and KEY not in json.dumps(served)
    served["config"]["url"] = AWAY

    result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "llm-o", "entity": served})

    assert result.is_error and _error_type(result) == "validation-error", result.output
    assert "re-enter the key" in result.output and KEY not in result.output
    row = sp.get_storage(LLMProvider)._data["llm-o"]
    assert str(row.config.url) == HOME and row.config.api_key.get_secret_value() == KEY, "the stored row is untouched"


@pytest.mark.asyncio
async def test_a_new_path_on_the_same_host_keeps_the_key(system_toolset, sp) -> None:
    await _create(system_toolset, _llm())
    served = await _served(system_toolset)
    served["config"]["url"] = "https://home.example/v2"

    result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "llm-o", "entity": served})

    assert not result.is_error, result.output
    row = sp.get_storage(LLMProvider)._data["llm-o"]
    assert str(row.config.url) == "https://home.example/v2" and row.config.api_key.get_secret_value() == KEY


@pytest.mark.asyncio
async def test_a_moved_host_with_a_new_key_stores_it(system_toolset, sp) -> None:
    await _create(system_toolset, _llm())
    served = await _served(system_toolset)
    served["config"]["url"] = AWAY
    served["config"]["api_key"] = "sk-live-a-different-key"

    result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "llm-o", "entity": served})

    assert not result.is_error, result.output
    row = sp.get_storage(LLMProvider)._data["llm-o"]
    assert str(row.config.url) == AWAY and row.config.api_key.get_secret_value() == "sk-live-a-different-key"


@pytest.mark.asyncio
async def test_an_embedding_provider_follows_the_same_rule(system_toolset, sp) -> None:
    body = {"id": "emb-o", "provider": "openai", "models": [{"name": "m"}], "config": {"url": HOME, "api_key": KEY}, "limits": {"max_concurrency": 1}}
    await _create(system_toolset, body, kind="embedding_provider")
    served = await _served(system_toolset, "emb-o", "embedding_provider")
    served["config"]["url"] = AWAY

    result = await system_toolset.call(tool_name="update_embedding_provider", arguments={"id": "emb-o", "entity": served})

    assert result.is_error and _error_type(result) == "validation-error", result.output
    assert sp.get_storage(EmbeddingProvider)._data["emb-o"].config.api_key.get_secret_value() == KEY


# ---- part B: create ---------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_refuses_the_served_body_of_another_row(system_toolset, sp) -> None:
    """The copy-a-provider move: ``get_llm_provider`` then ``create_llm_provider`` under a new id stored the masks as the url's password and the key."""
    await _create(system_toolset, _llm("http://svc:s3cr3t@px.lan/v1"))
    served = await _served(system_toolset)
    served["id"] = "copy"

    result = await system_toolset.call(tool_name="create_llm_provider", arguments={"entity": served}, ctx=ADMIN_CALLER)

    assert result.is_error and _error_type(result) == "validation-error", result.output
    assert "copy" not in sp.get_storage(LLMProvider)._data, "nothing was stored"


@pytest.mark.asyncio
async def test_create_refuses_a_masked_key_alone(system_toolset, sp) -> None:
    body = _llm("https://home.example/v1", "mk")
    body["config"]["api_key"] = "**********cdef"

    result = await system_toolset.call(tool_name="create_llm_provider", arguments={"entity": body}, ctx=ADMIN_CALLER)

    assert result.is_error and _error_type(result) == "validation-error", result.output
    assert "mk" not in sp.get_storage(LLMProvider)._data


@pytest.mark.asyncio
async def test_create_still_accepts_a_real_body(system_toolset, sp) -> None:
    await _create(system_toolset, _llm("http://svc:s3cr3t@px.lan/v1", "real"))

    assert "real" in sp.get_storage(LLMProvider)._data


# ---- part B: update_toolset answers instead of raising ---------------------------------------------------------------------------------------------------------------------


def _toolset(url: str, redirect: str, header: str | None = None) -> dict:
    http = {"url": url, "oauth": {"redirect_uri": redirect, "static_client": {"client_id": "c", "client_secret": "cs-123456"}}}
    if header is not None:
        http["headers"] = {"Authorization": header}
    return {"id": "ts-http", "provider": "mcp", "config": {"transport": "http", "config": http}}


@pytest.mark.asyncio
async def test_update_toolset_answers_a_redirect_uri_that_carries_the_mask_instead_of_raising(system_toolset, sp) -> None:
    """The check that decides who may write (``admin_when`` -> ``repoints_stored_secrets``) runs ``preserve_masked_secrets`` on a copy, and it used to raise out of the tool."""
    await _create(system_toolset, _toolset("http://127.0.0.1:9/mcp", "http://127.0.0.1:9/cb"), kind="toolset")
    body = _toolset("http://127.0.0.1:9/mcp", "http://u:**********@127.0.0.1:9/cb2")

    admin = await system_toolset.call(tool_name="update_toolset", arguments={"id": "ts-http", "entity": body}, ctx=ADMIN_CALLER)
    user = await system_toolset.call(tool_name="update_toolset", arguments={"id": "ts-http", "entity": body}, ctx=caller("user"))

    assert admin.is_error and _error_type(admin) == "validation-error", admin.output           # the mask cannot be restored: a refusal the agent can read
    assert user.is_error and _error_type(user) == "forbidden", user.output                      # the admin gate still answers first for a non-admin


@pytest.mark.asyncio
async def test_an_admin_who_moves_a_toolset_url_and_keeps_the_masked_header_is_refused(system_toolset) -> None:
    """The toolset rule used to let an ADMIN repoint with the stored secrets kept; an admin-started agent run is an admin caller, so the origin binding applies to every caller."""
    await _create(system_toolset, _toolset("https://home.example/mcp", "https://home.example/cb", "Bearer abcdef"), kind="toolset")
    served = await _served(system_toolset, "ts-http", "toolset")
    served["config"]["config"]["url"] = "https://attacker.example/mcp"

    result = await system_toolset.call(tool_name="update_toolset", arguments={"id": "ts-http", "entity": served}, ctx=ADMIN_CALLER)

    assert result.is_error and _error_type(result) == "validation-error", result.output
    assert "re-enter the key" in result.output and "abcdef" not in result.output


# ---- nit N1 (review of #711): a served mask where there is nothing stored of its shape ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_header_that_carries_a_served_mask_is_refused(system_toolset) -> None:
    """A header the stored row does not hold has nothing to restore the mask from: ``**********`` would be stored as its value."""
    await _create(system_toolset, _toolset("https://home.example/mcp", "https://home.example/cb", "Bearer abcdef"), kind="toolset")
    served = await _served(system_toolset, "ts-http", "toolset")
    served["config"]["config"]["headers"]["X-Extra"] = "**********"

    result = await system_toolset.call(tool_name="update_toolset", arguments={"id": "ts-http", "entity": served}, ctx=ADMIN_CALLER)

    assert result.is_error and _error_type(result) == "validation-error", result.output
    assert "X-Extra" in result.output and "abcdef" not in result.output


@pytest.mark.asyncio
async def test_a_provider_type_switch_that_sends_the_served_key_is_refused(system_toolset, sp) -> None:
    await _create(system_toolset, _llm())
    served = await _served(system_toolset)
    served["provider"] = "openresponses"
    served["config"]["url"] = AWAY

    result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "llm-o", "entity": served})

    assert result.is_error and _error_type(result) == "validation-error", result.output
    row = sp.get_storage(LLMProvider)._data["llm-o"]
    assert row.provider.value == "openchat" and row.config.api_key.get_secret_value() == KEY


# ---- part B: the descriptor -------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool_id", ["update_llm_provider", "update_embedding_provider", "update_toolset"])
@pytest.mark.asyncio
async def test_the_update_descriptor_tells_the_agent_when_a_mask_is_not_kept(system_toolset, tool_id: str) -> None:
    descriptions = {tool.id: tool.description async for tool in system_toolset.list_tools()}

    text = descriptions[tool_id]

    assert "validation-error" in text and "re-enter" in text, text
    assert "same" in text and "host" in text, "the descriptor says a stored secret is kept only for the same host"
