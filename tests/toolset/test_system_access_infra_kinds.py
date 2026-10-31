"""``get_entity_access`` / ``set_entity_access`` on grantable infrastructure agree with the REST sub-resource (access-control P4 Task 3).

The tool is built on the same ``set_access(use_only=True)`` the routes call, so the two doors cannot disagree (anti-goal 2).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.model.access import Access
from primer.model.model_profile import ModelProfile
from primer.model.principal import PrincipalRef
from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType, SqliteConfig
from primer.model.user import User
from primer.model.workspace import WorkspaceProvider
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset._system_access import _KINDS, _USE_ONLY, access_tools

INFRA = ["model_profile", "llm_provider", "workspace_provider"]


@pytest.fixture
async def tools(tmp_path: Path) -> AsyncIterator[tuple[dict, SqliteStorageProvider]]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await sp.initialize()
    try:
        for uid, role in (("root", "admin"), ("ada", "user")):
            await sp.get_storage(User).create(User(id=uid, username=uid, password_hash=None, created_at=datetime.now(UTC), role=role))
        await sp.get_storage(LLMProvider).create(
            LLMProvider(id="prov", provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("k")), limits=Limits(max_concurrency=1))
        )
        await sp.get_storage(ModelProfile).create(ModelProfile(id="prof", description="d", provider_id="prov", model_name="m", context_length=1000))
        await sp.get_storage(WorkspaceProvider).create(WorkspaceProvider.model_validate({"id": "wp", "provider": "local", "config": {"kind": "local"}}))
        yield access_tools(storage_provider=sp), sp
    finally:
        await sp.aclose()


IDS = {"model_profile": "prof", "llm_provider": "prov", "workspace_provider": "wp"}


def _ctx(uid: str, role: str) -> ToolContext:
    ref = PrincipalRef(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
    return ToolContext(tool_call_id="c", session_id="s", workspace_id="w", initiated_by=ref)


async def _call(tools, name, args, ctx):
    result = await tools[name][1](args, ctx=ctx)
    return result.is_error, json.loads(result.output)


def test_every_infrastructure_kind_is_use_only():
    assert set(_USE_ONLY) == {
        "model_profile", "llm_provider", "embedding_provider", "cross_encoder_provider", "semantic_search_provider",
        "workspace_provider", "channel_provider",
    } and _USE_ONLY <= set(_KINDS)


@pytest.mark.parametrize("kind", INFRA)
async def test_an_admin_grants_use_and_a_grantee_reads_it_through_the_tool(tools, kind):
    t, sp = tools
    err, view = await _call(t, "set_entity_access", {"kind": kind, "id": IDS[kind], "owners": [], "grants": {"ada": "read"}}, _ctx("root", "admin"))
    assert not err, view
    assert view["owners"] == [] and view["grants"][0]["user"]["id"] == "ada"
    err, view = await _call(t, "get_entity_access", {"kind": kind, "id": IDS[kind]}, _ctx("ada", "user"))
    assert not err and view["your_level"] == "read"


@pytest.mark.parametrize("kind", INFRA)
async def test_the_tool_refuses_what_the_route_refuses(tools, kind):
    t, _ = tools
    args = {"kind": kind, "id": IDS[kind]}
    assert (await _call(t, "set_entity_access", {**args, "owners": ["ada"]}, _ctx("root", "admin")))[0]  # owners
    assert (await _call(t, "set_entity_access", {**args, "owners": [], "grants": {"ada": "full"}}, _ctx("root", "admin")))[0]  # full
    err, body = await _call(t, "set_entity_access", {**args, "owners": [], "grants": {"ada": "read"}}, _ctx("ada", "user"))
    assert err and body["type"] == "not-found"  # a user with no use grant: the row reads as missing
    err, body = await _call(t, "get_entity_access", args, _ctx("ada", "user"))
    assert err and body["type"] == "not-found"
