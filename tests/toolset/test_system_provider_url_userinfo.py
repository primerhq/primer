"""The system provider tools serve a Base URL without its password and keep it through an update (ticket 01a11cdf part 3, option A).

``get_llm_provider`` (and ``create_*`` / ``update_*``) put the row in a TOOL RESULT, which is a model transcript: a ``config.url`` with ``user:password@`` sat there in clear next to a
masked ``api_key``. The result is the row's JSON dump, so the URL is masked like every other served copy, and ``update_llm_provider`` already runs ``preserve_masked_secrets`` (a field the
matching ``get_`` served masked and sent back unchanged keeps its stored value), which now covers the URL.
"""

from __future__ import annotations

import json

import pytest

from primer.model.provider import EmbeddingProvider, LLMProvider
from tests._support.caller import ADMIN_CALLER
from tests.toolset.test_system import pr, sp, system_toolset  # noqa: F401  (fixtures)

MASK = "**********"
PROXY = "http://svc:s3cr3t@proxy.local:8080/v1"
MASKED = f"http://svc:{MASK}@proxy.local:8080/v1"


def _llm_body(url: str = PROXY) -> dict:
    return {
        "id": "llm-px", "provider": "openchat", "models": [{"name": "m", "context_length": 8192}],
        "config": {"url": url, "api_key": "sk-live-0123456789", "flavor": "other"}, "limits": {"max_concurrency": 1},
    }


async def _create(toolset, body: dict, kind: str = "llm_provider"):
    return await toolset.call(tool_name=f"create_{kind}", arguments={"entity": body}, ctx=ADMIN_CALLER)


@pytest.mark.asyncio
async def test_create_and_get_serve_the_url_without_its_password(system_toolset, sp) -> None:
    created = await _create(system_toolset, _llm_body())
    assert not created.is_error, created.output

    got = await system_toolset.call(tool_name="get_llm_provider", arguments={"id": "llm-px"})

    for result in (created, got):
        assert "s3cr3t" not in result.output, result.output
    assert json.loads(got.output)["config"]["url"] == MASKED
    assert str(sp.get_storage(LLMProvider)._data["llm-px"].config.url) == PROXY, "stored real"


@pytest.mark.asyncio
async def test_list_serves_it_masked_too(system_toolset) -> None:
    await _create(system_toolset, _llm_body())

    listed = await system_toolset.call(tool_name="list_llm_providers", arguments={})

    assert "s3cr3t" not in listed.output and MASKED in listed.output, listed.output


@pytest.mark.asyncio
async def test_updating_with_the_returned_body_keeps_the_stored_password(system_toolset, sp) -> None:
    await _create(system_toolset, _llm_body())
    served = json.loads((await system_toolset.call(tool_name="get_llm_provider", arguments={"id": "llm-px"})).output)
    served["limits"]["max_concurrency"] = 5

    result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "llm-px", "entity": served})

    assert not result.is_error, result.output
    row = sp.get_storage(LLMProvider)._data["llm-px"]
    assert str(row.config.url) == PROXY and row.limits.max_concurrency == 5
    assert "s3cr3t" not in result.output


@pytest.mark.asyncio
async def test_updating_the_path_on_the_same_origin_with_the_mask_keeps_the_password(system_toolset, sp) -> None:
    await _create(system_toolset, _llm_body())
    served = json.loads((await system_toolset.call(tool_name="get_llm_provider", arguments={"id": "llm-px"})).output)
    served["config"]["url"] = f"http://svc:{MASK}@proxy.local:8080/v2"

    result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "llm-px", "entity": served})

    assert not result.is_error, result.output
    assert str(sp.get_storage(LLMProvider)._data["llm-px"].config.url) == "http://svc:s3cr3t@proxy.local:8080/v2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moved",
    [
        pytest.param(f"http://svc:{MASK}@attacker.example:9000/v2", id="another host"),
        pytest.param(f"http://other:{MASK}@proxy.local:8080/v1", id="another username"),
    ],
)
async def test_an_update_whose_mask_cannot_be_restored_is_refused_and_stores_nothing(system_toolset, sp, moved: str) -> None:
    """The twin of the REST rule, for the tool an agent run can call: pointing the URL at another host (the prompt-injection shape) with the mask left alone is refused."""
    await _create(system_toolset, _llm_body())
    served = json.loads((await system_toolset.call(tool_name="get_llm_provider", arguments={"id": "llm-px"})).output)
    served["config"]["url"] = moved

    result = await system_toolset.call(tool_name="update_llm_provider", arguments={"id": "llm-px", "entity": served})

    assert result.is_error, result.output
    assert "re-enter the password" in result.output and "s3cr3t" not in result.output, result.output
    assert str(sp.get_storage(LLMProvider)._data["llm-px"].config.url) == PROXY, "the stored row is untouched"


@pytest.mark.asyncio
async def test_an_embedding_provider_follows_the_same_rule(system_toolset, sp) -> None:
    body = {"id": "emb-px", "provider": "openai", "models": [{"name": "m"}], "config": {"url": PROXY}, "limits": {"max_concurrency": 1}}
    created = await _create(system_toolset, body, kind="embedding_provider")
    assert not created.is_error, created.output
    served = json.loads((await system_toolset.call(tool_name="get_embedding_provider", arguments={"id": "emb-px"})).output)
    assert served["config"]["url"] == MASKED

    result = await system_toolset.call(tool_name="update_embedding_provider", arguments={"id": "emb-px", "entity": served})

    assert not result.is_error, result.output
    assert str(sp.get_storage(EmbeddingProvider)._data["emb-px"].config.url) == PROXY
