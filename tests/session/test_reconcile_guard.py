"""The reconcile guard fails a test on ANY ERROR from session_reconcile -
not just the query-failure line it first caught (#718 nit: widen
reconcile_guard). A patch that raises ("failed to reconcile session") or a
refusal whose re-read fails turns the reconcile into a partial quiet no-op
for the affected session, which in a test means the storage fake does not
behave like a backend.

The check lives in :func:`reconcile_error_records` (a plain function, so
it can be unit-tested without calling the fixture directly); the autouse
fixture applies it to the test's caplog.
"""

from __future__ import annotations

import logging


def reconcile_error_records(records) -> list:
    """The session_reconcile ERROR records in *records* (a caplog record
    list): every record at ERROR or above logged by
    primer.workspace.session_reconcile - a query the reconcile could not
    make, a patch that raised, a refusal whose re-read failed. A non-empty
    list means the reconcile swallowed an error, and the test's storage
    fake does not behave like a backend."""
    return [
        record for record in records
        if record.name.startswith("primer.workspace.session_reconcile")
        and record.levelno >= logging.ERROR
    ]


class _CapLog:
    """A record list good enough for the check."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []

    def add(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _record(name: str, levelno: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=levelno, pathname=__file__, lineno=1,
        msg=message, args=None, exc_info=None,
    )


def test_the_guard_fails_on_a_non_query_error_from_session_reconcile() -> None:
    """A patch-level ERROR from the module (no "failed to query sessions"
    in the text) did not trip the old substring check - this is the
    #718 widen."""
    from tests._support.reconcile_guard import reconcile_error_records

    caplog = _CapLog()
    caplog.add(_record(
        "primer.workspace.session_reconcile", logging.ERROR,
        "session reconcile: failed to reconcile session s-x on w-y",
    ))
    assert reconcile_error_records(caplog.records)


def test_the_guard_still_fails_on_the_query_error() -> None:
    from tests._support.reconcile_guard import reconcile_error_records

    caplog = _CapLog()
    caplog.add(_record(
        "primer.workspace.session_reconcile", logging.ERROR,
        "session reconcile: failed to query sessions on w-y "
        "(reconciling the 3 already read)",
    ))
    assert reconcile_error_records(caplog.records)


def test_the_guard_passes_when_no_reconcile_error_was_logged() -> None:
    from tests._support.reconcile_guard import reconcile_error_records

    caplog = _CapLog()
    caplog.add(_record(
        "primer.workspace.session_reconcile", logging.WARNING,
        "session reconcile: session s-x on w-y was deleted before it could be reconciled",
    ))
    caplog.add(_record("primer.other.module", logging.ERROR, "an unrelated error"))
    assert reconcile_error_records(caplog.records) == []
