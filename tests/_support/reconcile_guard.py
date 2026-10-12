"""A guard for the tests whose storage fakes sit under the workspace-lost session reconcile (the #600 review).

``reconcile_sessions_to_workspace_lost`` is best-effort: a query it cannot make is logged ("failed to query sessions") and swallowed, and so are a patch that
raises and a refusal whose re-read fails - so a destroy or a probe tick never fails because of it. That is right in production and wrong in a test: a storage
fake that cannot answer the reconcile the way a backend does (it ignored the predicate, answered a cursor with an offset page, or was a ``MagicMock`` with no
``next_cursor``) turns the reconcile into a quiet no-op and the caller-level test passes for the wrong reason. Import ``reconcile_query_must_not_fail`` into a
test module and it fails any test in it during which ``primer.workspace.session_reconcile`` logged at ERROR or above - the #718 widen: not just the query line,
but any error the reconcile swallowed (the check itself is the plain :func:`reconcile_error_records`, unit-tested in ``tests/session/test_reconcile_guard.py``).
Tests that fail the query ON PURPOSE (``tests/session/test_session_reconcile.py``) do not import it.
"""

from __future__ import annotations

import logging

import pytest


def reconcile_error_records(records) -> list:
    """The session_reconcile ERROR records in *records* (a caplog record list):
    every record at ERROR or above logged by ``primer.workspace.session_reconcile``.
    Non-empty means the reconcile swallowed an error, and the test's storage fake
    does not behave like a backend."""
    return [
        record for record in records
        if record.name.startswith("primer.workspace.session_reconcile")
        and record.levelno >= logging.ERROR
    ]


@pytest.fixture(autouse=True)
def reconcile_query_must_not_fail(caplog):
    yield
    errors = reconcile_error_records(caplog.records)
    assert not errors, (
        "the session reconcile logged an ERROR and swallowed it: the storage fake in this test does not behave like a backend\n" + caplog.text
    )
