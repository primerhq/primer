"""For the tests that characterize what a workspace destroy does to the sessions it ENDS (``workspace_lost``).

A destroy now deletes the ended sessions too (GR-6, ruling D16), which leaves nothing to read their end reason from. These tests are about the end,
not the deletion (``tests/session/test_session_retention.py`` and ``tests/api/test_session_delete_and_destroy.py`` pin that), so they switch the
deletion off.
"""

from __future__ import annotations

from typing import Any

import pytest


async def _keep(*_args: Any, **_kwargs: Any) -> int:
    return 0


@pytest.fixture
def keep_destroyed_sessions(monkeypatch: Any) -> None:
    monkeypatch.setattr("primer.session.purge.purge_workspace_sessions", _keep)
