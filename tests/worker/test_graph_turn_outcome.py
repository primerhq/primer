"""The graph turn driver must report the graph's real outcome.

Observed live: a graph whose agent node failed ended its session
ended_reason=completed. The driver reported a fixed "graph_ended"
sentinel whatever happened, and the dispatch table maps that sentinel to
"completed" unconditionally - while the executor's actual outcome sat
unread in _last_ended_reason, the attribute that exists precisely
because the event stream cannot tell success from failure.
"""
from __future__ import annotations

from primer.model.workspace_session import SessionStatus
from primer.session.dispatch import _STOP_REASON_TO_STATUS
from primer.worker.drivers import _GraphTurnDriver


class _StubExecutor:
    def __init__(self, ended_reason: str) -> None:
        self._last_ended_reason = ended_reason
        self._workspace_session = None

    async def invoke(self, messages):
        if False:  # pragma: no cover - empty async generator
            yield None


async def test_driver_reports_failure_when_the_graph_failed():
    driver = _GraphTurnDriver(_StubExecutor("failed"))
    await driver.invoke([])
    assert driver.last_done_reason == "graph_failed"


async def test_driver_reports_graph_ended_on_success():
    driver = _GraphTurnDriver(_StubExecutor("completed"))
    await driver.invoke([])
    assert driver.last_done_reason == "graph_ended"


def test_dispatch_maps_graph_failed_to_a_failed_session():
    status, reason = _STOP_REASON_TO_STATUS["graph_failed"]
    assert status == SessionStatus.ENDED
    assert reason == "failed"


def test_dispatch_still_maps_graph_ended_to_completed():
    status, reason = _STOP_REASON_TO_STATUS["graph_ended"]
    assert status == SessionStatus.ENDED
    assert reason == "completed"


# ---- the path dispatch actually uses: the raw executor --------------------


def _bare_executor(ended_reason=None):
    """An executor instance without running __init__ (abstract hooks and
    constructor wiring are irrelevant to the outcome property)."""
    from primer.graph.base import _BaseGraphExecutor

    ex = object.__new__(GraphOutcomeProbe)
    if ended_reason is not None:
        ex._last_ended_reason = ended_reason
    return ex


from primer.graph.base import _BaseGraphExecutor  # noqa: E402


class GraphOutcomeProbe(_BaseGraphExecutor):
    async def _load_node_history(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def _persist_node_turn(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def _save_state(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


def test_executor_reports_no_outcome_mid_run():
    assert _bare_executor().last_done_reason is None


def test_executor_reports_graph_failed_after_a_failed_run():
    assert _bare_executor("failed").last_done_reason == "graph_failed"


def test_executor_reports_graph_ended_after_success():
    assert _bare_executor("completed").last_done_reason == "graph_ended"
