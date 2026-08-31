"""Pending realization at the drain checkpoint (S1 P1, plan Task 6b).

Spec: docs/superpowers/ux-revamp/02-s1-design.md section 4. A steer that
arrived mid-turn was queued instead of written; the checkpoint at the
end of the turn is where exactly one queued steer becomes a real turn.
"""

from datetime import UTC, datetime

import primer.session.dispatch as dispatch_mod
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import SessionDispatchDeps, _realize_pending_at_checkpoint


def _row(**kw) -> WorkspaceSession:
    base = {
        "id": "s",
        "workspace_id": "w",
        "binding": AgentSessionBinding(agent_id="a"),
        "status": SessionStatus.RUNNING,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return WorkspaceSession(**base)


def _deps(**kw) -> SessionDispatchDeps:
    base = dict(
        storage_provider=object(),
        workspace_io=object(),
        event_bus=object(),
        build_executor=lambda _row: None,
    )
    base.update(kw)
    return SessionDispatchDeps(**base)


async def test_realizes_one_pending_when_wiring_present(monkeypatch):
    calls = []

    async def _fake_realize(**kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(dispatch_mod, "realize_next_pending", _fake_realize)
    deps = _deps(scheduler=object(), claim_engine=object(),
                 workspace_registry=object())
    await _realize_pending_at_checkpoint(deps, _row())

    assert len(calls) == 1
    assert calls[0]["session_id"] == "s"
    assert calls[0]["workspace_id"] == "w"


async def test_noop_without_scheduler(monkeypatch):
    """Unit-test pools build deps without the wake wiring."""
    called = []

    async def _fake_realize(**kw):  # pragma: no cover - must not run
        called.append(kw)
        return True

    monkeypatch.setattr(dispatch_mod, "realize_next_pending", _fake_realize)
    await _realize_pending_at_checkpoint(_deps(workspace_registry=object()), _row())
    assert called == []


async def test_realization_failure_never_breaks_the_turn(monkeypatch):
    """The turn already terminated; a queue hiccup must not undo it."""

    async def _boom(**_kw):
        raise RuntimeError("storage down")

    monkeypatch.setattr(dispatch_mod, "realize_next_pending", _boom)
    deps = _deps(scheduler=object(), claim_engine=object(),
                 workspace_registry=object())
    await _realize_pending_at_checkpoint(deps, _row())  # must not raise

async def test_every_terminal_exit_drains_the_queue():
    """A queued steer is the user's message; losing it on a failed or
    cancelled turn drops work silently. Realization deletes the row and
    the queue is finite, so a failing session retries each queued
    message at most once rather than looping.
    """
    import inspect

    from primer.session.dispatch import run_one_session_turn

    src = inspect.getsource(run_one_session_turn)
    assert src.count("_realize_pending_at_checkpoint(deps, session)") == 3, (
        "expected the drain at all three terminal exits "
        "(executor failure, cancel/interrupt, clean completion)"
    )
