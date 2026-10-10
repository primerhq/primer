"""The system and workspaces list/find tools refuse a secret-bearing field (ticket 01a1212a).

The tools take ``order_by`` and ``predicate`` straight to storage, so a secret field there is a seek on the clear value a read masks. These tests
pin ``type=validation-error`` on the refusals and that plain fields still work, against a real SQLite backend.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import SecretStr

from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.provider import SqliteConfig
from primer.model.providers.llm import LLMProvider
from primer.model.workspace import WorkspaceTemplate
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.system import build_system_toolset
from primer.toolset.workspaces import build_workspaces_toolset
from tests._support.caller import ADMIN_CALLER


def _llm_body(i: int, pw: str) -> dict:
    return {
        "id": f"llm-{i}",
        "provider": "openchat",
        "models": [{"name": "m", "context_length": 8192}],
        "config": {"url": f"http://svc:{pw}@px.lan/v1", "api_key": f"sk-{pw}-1234", "flavor": "other"},
        "limits": {"max_concurrency": 1},
    }


def _forge_cursor(field: str, value) -> str:
    keys = [
        {"field": field, "value": value, "direction": "asc", "is_null": value is None},
        {"field": "id", "value": "", "direction": "asc", "is_null": False},
    ]
    payload = json.dumps({"keys": keys}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def _typ(result) -> str:
    return json.loads(result.output)["type"]


@pytest_asyncio.fixture
async def tools(tmp_path: Path) -> AsyncIterator[dict]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "secq.sqlite"))
    await sp.initialize()
    reg = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    system = build_system_toolset(storage_provider=sp, provider_registry=reg)
    workspaces = build_workspaces_toolset(
        storage_provider=sp, workspace_registry=WorkspaceRegistry(sp),
    )
    await sp.get_storage(LLMProvider).create(LLMProvider.model_validate(_llm_body(0, "alpha")))
    await sp.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate.model_validate(
            {"id": "tpl-a", "provider_id": "p-loc", "description": "d", "files": [], "env": {"K": "envsecretvalue"}}
        )
    )
    yield {"system": system, "workspaces": workspaces, "sp": sp}
    await sp.aclose()


@pytest.mark.asyncio
async def test_find_predicate_on_config_api_key_is_validation_error(tools) -> None:
    result = await tools["system"].call(
        tool_name="find_llm_providers",
        arguments={
            "predicate": {
                "kind": "predicate",
                "left": {"kind": "field", "name": "config.api_key"},
                "op": "~=",
                "right": {"kind": "value", "value": "sk-a%"},
            }
        },
        ctx=ADMIN_CALLER,
    )
    assert result.is_error
    assert _typ(result) == "validation-error", result.output


@pytest.mark.asyncio
async def test_list_order_by_config_url_is_validation_error(tools) -> None:
    result = await tools["system"].call(
        tool_name="list_llm_providers",
        arguments={"order_by": ["config.url:asc"]},
        ctx=ADMIN_CALLER,
    )
    assert result.is_error
    assert _typ(result) == "validation-error", result.output


@pytest.mark.asyncio
async def test_list_workspace_templates_order_by_env_is_validation_error(tools) -> None:
    result = await tools["workspaces"].call(
        tool_name="list_workspace_templates",
        arguments={"order_by": ["env:asc"]},
        ctx=ADMIN_CALLER,
    )
    assert result.is_error
    assert _typ(result) == "validation-error", result.output


@pytest.mark.asyncio
async def test_forged_cursor_naming_config_api_key_is_refused(tools) -> None:
    result = await tools["system"].call(
        tool_name="list_llm_providers",
        arguments={"cursor": _forge_cursor("config.api_key", "a")},
        ctx=ADMIN_CALLER,
    )
    assert result.is_error
    # A bad cursor is a client error, answered like the secret-field refusal
    # (nit 2), not type=storage-error.
    assert _typ(result) == "validation-error", result.output
    assert "sk-alpha" not in result.output


@pytest.mark.asyncio
async def test_a_bad_cursor_on_find_is_validation_error(tools) -> None:
    result = await tools["system"].call(
        tool_name="find_llm_providers",
        arguments={"predicate": None, "cursor": _forge_cursor("provider", "zzz")},
        ctx=ADMIN_CALLER,
    )
    assert result.is_error
    assert _typ(result) == "validation-error", result.output


@pytest.mark.asyncio
async def test_plain_find_and_order_still_work(tools) -> None:
    for i, pw in enumerate(["bravo", "charlie"], start=1):
        await tools["sp"].get_storage(LLMProvider).create(LLMProvider.model_validate(_llm_body(i, pw)))
    listed = await tools["system"].call(
        tool_name="list_llm_providers", arguments={"order_by": ["id:asc"]}, ctx=ADMIN_CALLER,
    )
    assert not listed.is_error, listed.output
    assert [r["id"] for r in json.loads(listed.output)["items"]] == ["llm-0", "llm-1", "llm-2"]

    found = await tools["system"].call(
        tool_name="find_llm_providers",
        arguments={
            "predicate": {
                "kind": "predicate",
                "left": {"kind": "field", "name": "provider"},
                "op": "=",
                "right": {"kind": "value", "value": "openchat"},
            }
        },
        ctx=ADMIN_CALLER,
    )
    assert not found.is_error, found.output
    assert len(json.loads(found.output)["items"]) == 3
