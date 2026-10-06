"""A failed release must not need the workspace it failed BECAUSE OF (ticket 01a1072f).

``on_release`` writes the failure's error record into the session's workspace. When the deployment refuses that
workspace, resolving it raises again, inside the release transaction: the release itself failed and the lease did not
drop. The record cannot be written anywhere durable (``messages.jsonl`` is IN the refused workspace), so the adapter logs
and carries on with the rest of the release.
"""

from __future__ import annotations

import logging

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.int.claim import ReleaseOutcome
from primer.model.workspace_refusal import WorkspaceRefusedError
from tests.claim.test_session_adapter import FakeStorage, _make_session


class _Registry:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def get_workspace(self, workspace_id: str):
        raise self._error


async def test_a_failed_release_survives_a_refused_workspace(caplog) -> None:
    sess = _make_session("s1")
    adapter = SessionClaimAdapter(
        session_storage=FakeStorage(sess),
        workspace_registry=_Registry(WorkspaceRefusedError("provider 'local' refused", provider_id="local")),
    )

    with caplog.at_level(logging.WARNING):
        await adapter.on_release(
            conn=None, entity_id="s1", outcome=ReleaseOutcome(success=False, last_error="boom", drop_lease=True),
        )

    assert any("refused" in r.getMessage() and "s1" in r.getMessage() for r in caplog.records)


async def test_any_other_failure_to_resolve_the_workspace_still_fails_the_release() -> None:
    """Only the refusal is tolerated: an unreachable workspace must still surface, as before."""
    sess = _make_session("s2")
    adapter = SessionClaimAdapter(session_storage=FakeStorage(sess), workspace_registry=_Registry(RuntimeError("gone")))

    with pytest.raises(RuntimeError, match="gone"):
        await adapter.on_release(
            conn=None, entity_id="s2", outcome=ReleaseOutcome(success=False, last_error="boom", drop_lease=True),
        )
