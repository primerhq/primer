"""Global tool-description conformance guard.

Builds EVERY internal toolset provider plus the sandbox/local workspace
tool descriptors and asserts each emitted tool obeys the make_tool
Purpose+When+Example anatomy (via ``assert_tool_conforms``).

``provider.list_tools()`` only iterates the registry; the handlers are
never invoked, so the providers are constructed with the same minimal
in-memory fakes the per-toolset tests use (imported from the sibling
test modules). A count floor guards against a toolset silently dropping
out of the registry.
"""

from __future__ import annotations

import pytest

from primer.agent.tool_manager import _workspace_tool_descriptor

# Sandbox + local workspace tool classes (the 7-tool surface, twinned).
from primer.workspace.sandbox.tools.ls import SandboxLs
from primer.workspace.sandbox.tools.read import SandboxRead
from primer.workspace.sandbox.tools.write import SandboxWrite
from primer.workspace.sandbox.tools.edit import SandboxEdit
from primer.workspace.sandbox.tools.glob import SandboxGlob
from primer.workspace.sandbox.tools.grep import SandboxGrep
from primer.workspace.sandbox.tools.exec_ import SandboxExec
from primer.workspace.local.tools.ls import Ls
from primer.workspace.local.tools.read import Read
from primer.workspace.local.tools.write import Write
from primer.workspace.local.tools.edit import Edit
from primer.workspace.local.tools.glob import Glob
from primer.workspace.local.tools.grep import Grep
from primer.workspace.local.tools.exec_ import Exec

# build_* factories.
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.toolset.misc import MISC_TOOLSET_ID, build_misc_toolset
from primer.toolset.search import SEARCH_TOOLSET_ID, build_search_toolset
from primer.toolset.harness import HARNESS_TOOLSET_ID, build_harness_toolset_provider
from primer.toolset.trigger import TRIGGER_TOOLSET_ID, build_trigger_toolset_provider
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from primer.toolset.system import SYSTEM_TOOLSET_ID, build_system_toolset
from primer.toolset import build_web_toolset

# Minimal fakes reused verbatim from the per-toolset test modules.
from tests.conftest import _FakeStorageProvider
from tests.toolset.test_harness_toolset import _SP as _HarnessSP, _EventBus
from tests.toolset.test_system import _SP as _SystemSP
from tests.toolset.test_workspaces import _SP as _WorkspacesSP, _StubBackend
from tests.toolset.test_search import stub_subsystem  # noqa: F401 - fixture reuse
from tests.toolset.web.test_factory import (
    _FakeWebFetchService,
    _FakeWebSearchService,
)

from tests.toolset._desc_conformance import assert_tool_conforms


def _build_providers():
    """Construct every internal toolset provider with minimal fakes.

    Mirrors the construction in each toolset's own conformance test so the
    wiring stays in lockstep. Handlers are never called by ``list_tools``,
    so the stubs only need to satisfy the build signatures.
    """
    from unittest.mock import AsyncMock, MagicMock

    # search: a MagicMock subsystem (as tests/toolset/test_search.py builds it).
    search_subsystem = MagicMock()
    search_subsystem.search = AsyncMock()

    # system: ProviderRegistry over the in-memory storage fake.
    system_sp = _SystemSP()
    system_pr = ProviderRegistry(
        system_sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )

    # workspaces: WorkspaceRegistry over the stub backend.
    workspaces_sp = _WorkspacesSP()
    workspace_registry = WorkspaceRegistry(workspaces_sp, factory=_StubBackend)

    return [
        build_misc_toolset(),
        build_search_toolset(search_subsystem),
        build_harness_toolset_provider(
            storage_provider=_HarnessSP(),
            event_bus=_EventBus(),
        ),
        build_trigger_toolset_provider(
            storage_provider=_FakeStorageProvider(),
            claim_engine=None,
            event_bus=None,
        ),
        build_workspaces_toolset(
            storage_provider=workspaces_sp,
            workspace_registry=workspace_registry,
        ),
        build_system_toolset(
            storage_provider=system_sp,  # type: ignore[arg-type]
            provider_registry=system_pr,
        ),
        # web: list_tools never calls handlers, so a fake search service +
        # a mock http client suffice (the mock needs no teardown).
        build_web_toolset(
            web_search_service=_FakeWebSearchService([]),
            web_fetch_service=_FakeWebFetchService(),
            http_client=MagicMock(),
        ),
    ]


@pytest.mark.asyncio
async def test_every_internal_toolset_conforms():
    providers = _build_providers()
    seen = 0
    seen_ids: set[str] = set()
    for provider in providers:
        async for tool in provider.list_tools():
            assert_tool_conforms(tool)
            seen_ids.add(tool.toolset_id)
            seen += 1
    # The id-set is the real guard: it catches ANY provider silently
    # dropping out of the registry, regardless of its tool count, and is
    # not brittle to tool-count changes. The seen count floor is a coarse
    # secondary sanity check (~159 internal tools across the seven toolsets).
    expected_ids = {
        MISC_TOOLSET_ID,
        SEARCH_TOOLSET_ID,
        HARNESS_TOOLSET_ID,
        TRIGGER_TOOLSET_ID,
        WORKSPACES_TOOLSET_ID,
        SYSTEM_TOOLSET_ID,
        "web",
    }
    assert seen_ids == expected_ids, f"toolset coverage gap: {expected_ids ^ seen_ids}"
    assert seen > 130, f"only {seen} tools seen; a toolset may have dropped out"


def test_workspace_tool_descriptors_conform():
    classes = [
        SandboxLs, SandboxRead, SandboxWrite, SandboxEdit,
        SandboxGlob, SandboxGrep, SandboxExec,
        Ls, Read, Write, Edit, Glob, Grep, Exec,
    ]
    for cls in classes:
        inst = cls.__new__(cls)  # parameters() is pure; skip __init__
        tool = _workspace_tool_descriptor(inst, scoped_id=f"workspace__{cls.id}")
        assert_tool_conforms(tool)
