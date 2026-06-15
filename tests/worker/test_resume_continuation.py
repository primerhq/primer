"""Tests for the pure ``resume_continuation`` continuation walk.

These exercise the unwind logic in isolation, using *fake* frames. The
innermost frame resolves its OWN leaf via ``resume_leaf`` (returning a
``Completed``/``Reparked`` directly); the outer frames are driven via
``resume``. No pool / storage / frame-rehydration wiring is involved -
this is the pure walk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from primer.model.chat import ToolResultPart
from primer.worker.continuation import (
    Deliver,
    InvocationServices,
    Repark,
    resume_continuation,
)
from primer.worker.frames import Completed, Reparked


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeFrame:
    """A frame whose ``resume``/``resume_leaf`` return pre-scripted outcomes.

    ``resume_outcome`` is what ``resume`` returns (outer frames); when the
    frame is the innermost, ``leaf_outcome`` is what ``resume_leaf`` returns.
    Both record their received args for assertions.
    """

    resume_outcome: Any = None
    leaf_outcome: Any = None
    received: Any = None
    received_services: Any = None
    leaf_received: Any = None

    async def resume(self, child_result: Any, services: Any) -> Any:
        self.received = child_result
        self.received_services = services
        return self.resume_outcome

    async def resume_leaf(self, leaf: Any, payload: Any, services: Any) -> Any:
        self.leaf_received = (leaf, payload, services)
        return self.leaf_outcome


@dataclass
class FakeYield:
    """Stand-in for YieldToWorker: carries a nested frame stack + a new leaf."""

    frames: list
    yielded: Any


def _trp(output: str) -> ToolResultPart:
    return ToolResultPart(id="tc", output=output, error=False)


def _services() -> InvocationServices:
    return InvocationServices(
        build_subagent_toolmanager=lambda *a, **k: None,
        resume_subagent=lambda *a, **k: None,
        resolve_graph=lambda *a, **k: None,
        build_child_graph_executor=lambda *a, **k: None,
        graph_agent_tool_result=lambda *a, **k: None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwind_two_frames_delivers():
    inner_done = _trp("inner-done")
    outer_done = _trp("outer-done")

    # innermost resolves its leaf -> Completed(inner_done)
    inner = FakeFrame(leaf_outcome=Completed(value=inner_done))
    outer = FakeFrame(resume_outcome=Completed(value=outer_done))
    frames = [outer, inner]

    leaf = object()
    out = await resume_continuation(frames, leaf=leaf, payload={}, services=_services())

    assert isinstance(out, Deliver)
    assert out.tool_result.output == "outer-done"
    # inner.resume_leaf got the leaf; outer.resume got inner's result.
    assert inner.leaf_received[0] is leaf
    assert outer.received is inner_done


@pytest.mark.asyncio
async def test_repark_midunwind_preserves_outer_frames():
    f0 = FakeFrame(resume_outcome=Completed(value=_trp("f0")))  # never resumed
    NF = object()
    NL = object()
    f1 = FakeFrame(resume_outcome=Reparked(new_yield=FakeYield(frames=[NF], yielded=NL)))
    f2 = FakeFrame(leaf_outcome=Completed(value=_trp("f2-done")))
    frames = [f0, f1, f2]

    out = await resume_continuation(frames, leaf=object(), payload={}, services=_services())

    assert isinstance(out, Repark)
    assert out.frames == [f0, NF]
    assert out.leaf is NL
    # f0 (outer of f1) was never resumed.
    assert f0.received is None


@pytest.mark.asyncio
async def test_resume_leaf_repark_at_innermost():
    f0 = FakeFrame(resume_outcome=Completed(value=_trp("f0")))
    NF = object()
    NL = object()
    f1 = FakeFrame(leaf_outcome=Reparked(new_yield=FakeYield(frames=[NF], yielded=NL)))
    frames = [f0, f1]

    out = await resume_continuation(frames, leaf=object(), payload={}, services=_services())

    assert isinstance(out, Repark)
    assert out.frames == [f0, NF]
    assert out.leaf is NL
    # The outer frame was never resumed - we re-parked at the leaf.
    assert f0.received is None


@pytest.mark.asyncio
async def test_single_frame_delivers():
    only = FakeFrame(leaf_outcome=Completed(value=_trp("done")))
    frames = [only]

    leaf = object()
    out = await resume_continuation(frames, leaf=leaf, payload={}, services=_services())

    assert isinstance(out, Deliver)
    assert out.tool_result.output == "done"
    assert only.leaf_received[0] is leaf


@pytest.mark.asyncio
async def test_empty_frames_rejected():
    with pytest.raises(AssertionError):
        await resume_continuation([], leaf=object(), payload={}, services=_services())
