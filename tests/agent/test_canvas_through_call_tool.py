"""The canvas tools work through ``call_tool`` (pivot P5, task 11): round-trip pins.

The canvas toolset (P5 S3, PR #795) and the ``call_tool`` door (P1 slice 2, PR #787) are both merged, so a
USER-role run whose only tool is ``system__call_tool`` reaches the canvas tools and, through them, the
caller's own session:

* ``canvas_apply`` writes the CALLING session's canvas: ``call_tool`` hands the inner tool the caller's
  own ``ToolContext`` (P1 task 2), so the canvas is the one of the run, not of some other session;
* ``canvas_wait`` parks the turn under the ``call_tool`` call's id, the way the other pausing tools do
  through the same path (P1 task 3, ``tests/agent/test_call_tool_pauses.py``): the inner provider stamps
  the inner tool's name on the ``Yielded``, and the ``YieldToWorker`` carries the outer call's id, so the
  park is written under ``call-outer`` and the wake comes back to that same ``call_tool`` call.

These are pins: no production change is expected (if one fails, report it rather than changing code). The
manager is wired the way P1 task 3 wires it: a real ``ProviderRegistry`` with the system and canvas
toolsets, over the in-memory storage provider (which has an event store, which ``canvas_wait`` needs).

The wait pin names the element ``ok``, the way the canvas test world of
``tests/toolset/test_canvas_wait.py`` holds it: a wait on an id the canvas does not hold is refused at
once (pinned there), so the pin gives the canvas that element first (a fixture precondition, not a
production change).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.api.registries import ProviderRegistry
from primer.canvas.lang import parse_program
from primer.model.chat import ToolCallPart
from primer.model.yield_ import YieldToWorker
from primer.toolset.canvas import build_canvas_toolset
from primer.toolset.system import build_system_toolset
from tests.agent.test_inner_dispatch import USER, _Session


@pytest.fixture
def canvas_ts(fake_storage_provider: Any) -> Any:
    """The real canvas toolset over the in-memory storage provider (a bare build: no bus, no workspace registry)."""
    return build_canvas_toolset(storage_provider=fake_storage_provider)


@pytest.fixture
def registry(fake_storage_provider: Any, canvas_ts: Any) -> ProviderRegistry:
    """The wiring of ``tests/agent/test_call_tool_pauses.py`` (P1 task 3) plus the canvas toolset
    ``primer/api/_app_lifespan.py`` builds on startup."""
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
    registry._canvas_toolset_provider = canvas_ts  # noqa: SLF001
    return registry


@pytest.fixture
async def mgr(registry: ProviderRegistry, canvas_ts: Any) -> ToolExecutionManager:
    """The manager of a USER-role agent whose only tool is ``system__call_tool``; the canvas toolset is
    reachable through it, as the plan's interface says."""
    return ToolExecutionManager.for_workspace(
        toolset_providers={"system": await registry.get_toolset("system"), "canvas": canvas_ts},
        session=_Session("s-1", "ws-1"),
        tools=["system__call_tool"],
        initiated_by=USER,
    )


@pytest.fixture
async def seeded_canvas(mgr: ToolExecutionManager, canvas_ts: Any) -> Any:
    """The canvas the plan's wait pin names: a fresh canvas holds only ``root``, and a wait on an id it
    does not hold is refused at once (pinned in ``tests/toolset/test_canvas_wait.py``), so the pin's
    ``ids`` must name an element the canvas holds. The element ``ok`` is the canvas test world's."""
    await canvas_ts.service.apply(
        session_id="s-1", workspace_id="ws-1",
        statements=parse_program('root = Column([ok])\nok = Button("OK", "primary")'),
        unit="line", base_rev=None, source="agent", by="agent",
    )
    return canvas_ts


def _outer(tool_name: str, arguments: dict[str, Any]) -> ToolCallPart:
    """The model's ``system__call_tool`` call into the canvas toolset, under the id its ``tool_use`` was given."""
    return ToolCallPart(
        id="call-outer",
        name="system__call_tool",
        arguments={"toolset_id": "canvas", "tool_name": tool_name, "arguments": arguments},
    )


async def test_canvas_apply_through_call_tool_draws_on_the_calling_session(
    mgr: ToolExecutionManager, canvas_ts: Any,
) -> None:
    out = await mgr.execute(_outer("canvas_apply", {"source": 'root = Column([t])\nt = Text("hi")'}))
    assert json.loads(out.output)["rev"] == 1
    assert "t" in (await canvas_ts.service.snapshot("s-1", workspace_id="ws-1")).elements


async def test_canvas_wait_through_call_tool_parks_under_the_call_tool_call_id(
    seeded_canvas: Any, mgr: ToolExecutionManager,
) -> None:
    with pytest.raises(YieldToWorker) as exc:
        await mgr.execute(_outer("canvas_wait", {"ids": ["ok"]}))
    assert exc.value.tool_call_id == "call-outer" and exc.value.yielded.tool_name == "canvas_wait"
    assert exc.value.yielded.event_key == "canvaswait:s-1:call-outer"
