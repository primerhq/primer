"""A sub-agent's collection reads are the RUN's person's (access-control P4 Task 6, pins for the Phase 3 tool rule).

``invoke_agent`` hands the parent run's identity to the sub-agent's manager, and the ``collections`` toolset reads as the ``initiated_by`` of the
context the manager dispatches with: a sub-agent never reads a private collection its parent's person cannot, and reads one the person may.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from primer.agent.invoke import build_subagent_toolmanager
from primer.knowledge.tree import DocumentTreeService
from primer.model.access import Access
from primer.model.chat import ToolCallPart
from primer.model.collection import Collection
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.collections import build_collections_toolset
from primer.worker.frames import AgentResumeContext

TOOLS = ["collections__read_document"]


def _ref(uid: str, role: str = "user") -> PrincipalRef:
    return PrincipalRef(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)


class _Registry:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def get_toolset(self, toolset_id: str) -> Any:
        return self.provider


@pytest.fixture
async def provider(tmp_path: Path) -> AsyncIterator[Any]:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    for uid in ("alice", "bob"):
        await sp.get_storage(User).create(User(id=uid, username=uid, created_at=datetime.now(UTC), role="user"))
    await sp.get_storage(Collection).create(Collection(id="kb", description="wiki", access=Access(owners=["alice"])))
    await DocumentTreeService(sp).create(collection_id="kb", parent="", slug="a", body="secret body")
    yield build_collections_toolset(storage_provider=sp)
    await sp.aclose()


async def _read_as(provider: Any, who: PrincipalRef) -> Any:
    context = AgentResumeContext("s-1", "ws-1", None, None, list(TOOLS), initiated_by=who)
    manager = await build_subagent_toolmanager(context, provider_registry=_Registry(provider))
    assert manager.context_for("c1").initiated_by == who, "the sub-agent runs as the person its parent run acts as"
    return await manager.execute(ToolCallPart(id="c1", name="collections__read_document", arguments={"collection": "kb", "path": "a"}))


async def test_subagent_reads_as_the_parents_person(provider):
    owner = await _read_as(provider, _ref("alice"))
    stranger = await _read_as(provider, _ref("bob"))

    assert owner.error is False and "secret body" in owner.output
    assert stranger.error is True and json.loads(stranger.output)["type"] == "not-found"
    assert "secret body" not in stranger.output


async def test_a_run_with_no_initiator_reads_as_an_unattributed_user_not_the_system(provider):
    context = AgentResumeContext("s-1", "ws-1", None, None, list(TOOLS), initiated_by=None)
    manager = await build_subagent_toolmanager(context, provider_registry=_Registry(provider))

    result = await manager.execute(ToolCallPart(id="c1", name="collections__read_document", arguments={"collection": "kb", "path": "a"}))

    assert result.error is True, "an unattributed run reads nothing private; it must not fall back to the system principal"
