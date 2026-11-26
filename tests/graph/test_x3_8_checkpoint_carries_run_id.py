"""X3.8: the park checkpoint carries the executor's run identity, so a
park-resume rebuild of the child binds the ORIGINAL child's identity: the
resumed run's state saves stay keyed to the run that parked (a later reclaim of
the same child resumes it instead of starting fresh), and a checkpoint written
before the field existed keeps the rebuild's own key (degrade-gracefully, as
every other new checkpoint field).
"""

from pathlib import Path

import pytest

from tests.graph.test_review_6_2_reclaim_resume_shapes import _StubStateRepo
from tests.graph.test_review_k2_round1_resume import _build, _linear


def _fresh_attrs(ex) -> None:
    """The mid-flight attrs a constructed executor does not hold yet (they are
    set at the first superstep): emptied here so the snapshot is the shape of
    an empty park and ``snapshot_state`` needs nothing more than ``_run_id``."""
    ex._context = None
    ex._ready_set = set()
    ex._node_states = {}
    ex._pending_toolcalls = []
    ex._pending_agent_yields = []
    ex._pending_tool_waits = []
    ex._pending_ended_reason = None
    ex._pending_ended_detail = None
    ex._max_iter_routed = False
    ex._fanout_instances = {}
    ex._fanout_target_expected_count = {}
    ex._instance_to_spec = {}
    ex._fanout_drain_state = {}


@pytest.mark.asyncio
async def test_the_park_checkpoint_carries_the_run_id_and_the_rebuild_binds_it(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    ex = await _build(repo, {}, "gs", _linear(0), run_id="turn-1/agent@2/invoke#1")
    _fresh_attrs(ex)
    snap = ex.snapshot_state()
    assert snap["run_id"] == "turn-1/agent@2/invoke#1"

    # The park-resume rebuild: a fresh executor (its own constructor's run
    # key) for the same child subtree.
    repo2 = _StubStateRepo(tmp_path / "r2" / ".state", "ws")
    await repo2.initialize()
    ex2 = await _build(repo2, {}, "gs", _linear(0), turn_no=5)
    assert ex2._run_id == "turn-5"
    ex2.restore_state(snap)
    assert ex2._run_id == "turn-1/agent@2/invoke#1"


@pytest.mark.asyncio
async def test_a_checkpoint_that_predates_the_field_keeps_the_rebuilds_own_run_key(tmp_path: Path) -> None:
    repo = _StubStateRepo(tmp_path / ".state", "ws")
    await repo.initialize()
    ex = await _build(repo, {}, "gs", _linear(0), run_id="turn-1/agent@2/invoke#1")
    _fresh_attrs(ex)
    snap = ex.snapshot_state()
    del snap["run_id"]

    repo2 = _StubStateRepo(tmp_path / "r2" / ".state", "ws")
    await repo2.initialize()
    ex2 = await _build(repo2, {}, "gs", _linear(0), turn_no=5)
    ex2.restore_state(snap)
    assert ex2._run_id == "turn-5"
