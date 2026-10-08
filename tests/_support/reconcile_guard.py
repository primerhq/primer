"""A guard for the tests whose storage fakes sit under the workspace-lost session reconcile (the #600 review).

``reconcile_sessions_to_workspace_lost`` is best-effort: a query it cannot make is logged ("failed to query sessions") and swallowed, so a destroy or a
probe tick never fails because of it. That is right in production and wrong in a test: a storage fake that cannot answer ``find()`` the way a backend does
(it ignored the predicate, answered a cursor with an offset page, or was a ``MagicMock`` with no ``next_cursor``) turns the reconcile into a quiet no-op
and the caller-level test passes for the wrong reason. Import ``reconcile_query_must_not_fail`` into a test module and it fails any test in it during which
that line was logged. Tests that fail the query ON PURPOSE (``tests/session/test_session_reconcile.py``) do not import it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reconcile_query_must_not_fail(caplog):
    yield
    assert "failed to query sessions" not in caplog.text, (
        "the session reconcile could not query the sessions and swallowed it: the storage fake in this test does not behave like a backend\n" + caplog.text
    )
