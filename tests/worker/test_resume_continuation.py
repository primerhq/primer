"""Tests for the pure ``resume_continuation`` continuation walk.

These exercise the unwind logic in isolation, using *fake* frames (objects
with an async ``resume`` returning ``Completed``/``Reparked``) and a
monkeypatched ``apply_leaf``. No pool / storage / frame-rehydration wiring
is involved - this is the pure walk (Task 3.3a).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

import primer.worker.continuation as cont
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
    """A frame whose ``resume`` returns a pre-scripted outcome and records args."""

    outcome: Any
    received: Any = None
    received_services: Any = None

    async def resume(self, child_result: Any, services: Any) -> Any:
        self.received = child_result
        self.received_services = services
        return self.outcome


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
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwind_two_frames_delivers(monkeypatch):
    leaf_result = _trp("leaf")
    inner_done = _trp("inner-done")
    outer_done = _trp("outer-done")

    inner = FakeFrame(outcome=Completed(value=inner_done))
    outer = FakeFrame(outcome=Completed(value=outer_done))
    frames = [outer, inner]

    async def fake_apply_leaf(inner_frame, leaf, payload, services):
        assert inner_frame is inner  # innermost
        return leaf_result

    monkeypatch.setattr(cont, "apply_leaf", fake_apply_leaf)

    out = await resume_continuation(frames, leaf=object(), payload={}, services=_services())

    assert isinstance(out, Deliver)
    assert out.tool_result.output == "outer-done"
    # inner.resume got the leaf result; outer.resume got inner's result.
    assert inner.received is leaf_result
    assert outer.received is inner_done


@pytest.mark.asyncio
async def test_repark_midunwind_preserves_outer_frames(monkeypatch):
    f0 = FakeFrame(outcome=Completed(value=_trp("f0")))  # never resumed
    NF = object()
    NL = object()
    f1 = FakeFrame(outcome=Reparked(new_yield=FakeYield(frames=[NF], yielded=NL)))
    f2 = FakeFrame(outcome=Completed(value=_trp("f2-done")))
    frames = [f0, f1, f2]

    async def fake_apply_leaf(inner_frame, leaf, payload, services):
        return _trp("leaf")

    monkeypatch.setattr(cont, "apply_leaf", fake_apply_leaf)

    out = await resume_continuation(frames, leaf=object(), payload={}, services=_services())

    assert isinstance(out, Repark)
    assert out.frames == [f0, NF]
    assert out.leaf is NL
    # f0 (outer of f1) was never resumed.
    assert f0.received is None


@pytest.mark.asyncio
async def test_apply_leaf_repark_at_innermost(monkeypatch):
    f0 = FakeFrame(outcome=Completed(value=_trp("f0")))
    f1 = FakeFrame(outcome=Completed(value=_trp("f1")))
    frames = [f0, f1]
    NF = object()
    NL = object()

    async def fake_apply_leaf(inner_frame, leaf, payload, services):
        return Reparked(new_yield=FakeYield(frames=[NF], yielded=NL))

    monkeypatch.setattr(cont, "apply_leaf", fake_apply_leaf)

    out = await resume_continuation(frames, leaf=object(), payload={}, services=_services())

    assert isinstance(out, Repark)
    assert out.frames == [f0, NF]
    assert out.leaf is NL
    # Neither frame was resumed - we re-parked at the leaf.
    assert f0.received is None
    assert f1.received is None


@pytest.mark.asyncio
async def test_single_frame_delivers(monkeypatch):
    only = FakeFrame(outcome=Completed(value=_trp("done")))
    frames = [only]
    leaf_result = _trp("leaf")

    async def fake_apply_leaf(inner_frame, leaf, payload, services):
        return leaf_result

    monkeypatch.setattr(cont, "apply_leaf", fake_apply_leaf)

    out = await resume_continuation(frames, leaf=object(), payload={}, services=_services())

    assert isinstance(out, Deliver)
    assert out.tool_result.output == "done"
    assert only.received is leaf_result


@pytest.mark.asyncio
async def test_empty_frames_rejected():
    with pytest.raises(AssertionError):
        await resume_continuation([], leaf=object(), payload={}, services=_services())
