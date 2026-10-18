"""A tool that pauses the turn works through ``call_tool``, and its answer lands on the ``call_tool`` call (pivot P1, task 3).

``system__call_tool`` hands the inner tool the caller's OWN ``ToolContext`` (task 2), so the tools that pause a turn (``system__ask_user``,
``system__wait_for_event``, ``workspace_ext__sleep``) can be reached through it:

* the inner provider stamps the INNER tool's name on the ``Yielded`` it raises, and the ``YieldToWorker`` carries the ``call_tool`` call's id
  (the ``tool_use`` the model is waiting on), so the park is written under that id and the session's own;
* the resume path (``primer.worker.yield_resume_registry``) finds the hook by the inner tool's name, and what the hook returns is delivered to
  that same ``call_tool`` call.

Before the pivot the handler passed no context and ``call_tool`` was refused to every user-role run, so a pausing inner tool could not run at
all. These tests drive the real tool manager and the real system, misc and workspace_ext toolsets, as an agent's turn does, for a USER-role
run. The approved re-run of a gated pausing tool is pinned in ``tests/worker/test_call_tool_approval_rerun.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.api.registries import ProviderRegistry
from primer.model.chat import ToolCallPart
from primer.model.event import EventSubscription
from primer.model.storage import OffsetPage
from primer.model.yield_ import YieldToWorker
from primer.toolset.misc import build_misc_toolset
from primer.toolset.system import build_system_toolset
from primer.toolset.workspace_ext import build_workspace_ext_toolset
from primer.worker.yield_resume_registry import ResumeContext, get_resume_hook
from tests.agent.test_inner_dispatch import USER, _Session


@pytest.fixture
def registry(fake_storage_provider: Any) -> ProviderRegistry:
    """A real ``ProviderRegistry`` with the three toolsets these tests reach wired on, as ``primer/api/_app_lifespan.py`` does (the same wiring
    ``tests/mcp/test_required_role_completeness.py`` builds), over the in-memory storage provider (which has an event store)."""
    registry = ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
    )
    registry._system_toolset_provider = build_system_toolset(  # noqa: SLF001
        storage_provider=fake_storage_provider, provider_registry=registry,
    )
    registry._misc_toolset_provider = build_misc_toolset()  # noqa: SLF001
    registry._workspace_ext_toolset_provider = build_workspace_ext_toolset(storage_provider=fake_storage_provider)  # noqa: SLF001
    return registry


@pytest.fixture
async def mgr(registry: ProviderRegistry) -> ToolExecutionManager:
    """The manager of a USER-role agent whose only tool is ``system__call_tool``: it holds the system toolset, as a session's manager does, and
    reaches every other toolset through ``call_tool`` and the registry."""
    return ToolExecutionManager.for_workspace(
        toolset_providers={"system": await registry.get_toolset("system")},
        session=_Session("s-1", "ws-1"),
        tools=["system__call_tool"],
        initiated_by=USER,
    )


def _outer(toolset_id: str, tool_name: str, arguments: dict[str, Any]) -> ToolCallPart:
    """The model's ``system__call_tool`` call, under the id its ``tool_use`` was given."""
    return ToolCallPart(
        id="call-outer",
        name="system__call_tool",
        arguments={"toolset_id": toolset_id, "tool_name": tool_name, "arguments": arguments},
    )


async def test_ask_user_through_call_tool_parks_under_the_call_tool_call_id(mgr: ToolExecutionManager) -> None:
    with pytest.raises(YieldToWorker) as exc:
        await mgr.execute(_outer("system", "ask_user", {"prompt": "Proceed?"}))
    assert exc.value.tool_call_id == "call-outer"
    assert exc.value.yielded.tool_name == "ask_user", "the park names the INNER tool: the resume path finds its hook by this name"
    assert "s-1" in exc.value.yielded.event_key and "call-outer" in exc.value.yielded.event_key
    assert exc.value.yielded.resume_metadata["tool_call_id"] == "call-outer", "the question's record names the call the answer is for"

    # What the worker does when the answer arrives: the hook found by the parked tool name turns it into the result of the park's call.
    out = get_resume_hook(exc.value.yielded.tool_name)(
        exc.value.yielded.resume_metadata,
        {"response": "yes"},
        ResumeContext(tool_name="ask_user", tool_call_id=exc.value.tool_call_id, session_id="s-1"),
    )
    assert not out.is_error and json.loads(out.output) == {"response": "yes"}


async def test_sleep_through_call_tool_parks_on_the_session(mgr: ToolExecutionManager) -> None:
    with pytest.raises(YieldToWorker) as exc:
        await mgr.execute(_outer("workspace_ext", "sleep", {"seconds": 5}))
    assert (exc.value.tool_call_id, exc.value.yielded.tool_name) == ("call-outer", "sleep")
    assert exc.value.yielded.resume_metadata["requested_seconds"] == 5.0
    assert exc.value.yielded.event_key == "timer:s-1:call-outer", "the timer is the session's, so no other session's timer wakes this park"


async def test_wait_for_event_through_call_tool_subscribes_for_the_session(
    mgr: ToolExecutionManager, fake_storage_provider: Any,
) -> None:
    # A user-role run may wait only on the kinds the event feed shows it (primer/events/visibility.py): ``session.*`` is one.
    with pytest.raises(YieldToWorker) as exc:
        await mgr.execute(_outer("system", "wait_for_event", {"event_types": ["session.*"]}))
    assert (exc.value.tool_call_id, exc.value.yielded.tool_name) == ("call-outer", "wait_for_event")
    subs = (await fake_storage_provider.get_storage(EventSubscription).list(OffsetPage(offset=0, length=50))).items
    mine = [s for s in subs if getattr(s.sink, "session_id", None) == "s-1"]
    assert len(mine) == 1, "one subscription, for this session"
    assert mine[0].sink.event_key == exc.value.yielded.event_key and "call-outer" in exc.value.yielded.event_key, (
        "the wake is delivered to the park written under the call_tool call"
    )
