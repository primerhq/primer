"""``sample_claim_gauges_once``: one pass over the leases table sets both claim gauges for EVERY kind.

The sampler used to run ``SELECT kind, COUNT(*) ... WHERE claimed_by IS NULL GROUP BY kind`` and set only the kinds that came
back. A kind whose queue drained has no row, so ``claim_queue_depth`` kept its last non-zero value for as long as nothing was
queued under that kind: an idle system reported a backed-up queue. ``claim_active_count`` had no writer at all. The real SQL is
covered against a live database in ``tests/claim/test_postgres_claim_gauges.py``; these tests pin what the sampler does with the
rows it gets back, with no database.
"""

from __future__ import annotations

import pytest

import primer.observability.metrics as m
import primer.api._app_lifespan_phases as phases
from primer.int.claim import ClaimKind


@pytest.fixture(autouse=True)
def _fresh_metrics():
    m.reset_for_test()
    yield
    m.reset_for_test()


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, _sql, *_args):
        return self._rows


class _Acquire:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return _Conn(self._rows)

    async def __aexit__(self, *_exc):
        return False


class _Pool:
    def __init__(self, rows):
        self._rows = rows

    def acquire(self):
        return _Acquire(self._rows)


class _Storage:
    def __init__(self, rows):
        self.pool = _Pool(rows)


class _Engine:
    """Just the two attributes the sampler reads off a Postgres claim engine."""

    _table = "leases"

    def __init__(self, rows):
        self._storage = _Storage(rows)


def _queued(kind: str) -> float:
    return m.claim_queue_depth.labels(kind)._value.get()


def _active(kind: str) -> float:
    return m.claim_active_count.labels(kind)._value.get()


@pytest.mark.asyncio
async def test_a_kind_whose_queue_drained_returns_to_zero():
    await phases.sample_claim_gauges_once(_Engine([{"kind": "session", "queued": 5, "active": 0}]))
    assert _queued("session") == 5.0

    # Every lease of that kind was claimed, so the table has no row for it any more.
    await phases.sample_claim_gauges_once(_Engine([]))

    assert _queued("session") == 0.0


@pytest.mark.asyncio
async def test_queued_and_active_are_set_per_kind_from_the_rows():
    rows = [
        {"kind": "session", "queued": 3, "active": 2},
        {"kind": "harness", "queued": 0, "active": 4},
    ]

    await phases.sample_claim_gauges_once(_Engine(rows))

    assert (_queued("session"), _active("session")) == (3.0, 2.0)
    assert (_queued("harness"), _active("harness")) == (0.0, 4.0)


@pytest.mark.asyncio
async def test_every_kind_is_set_even_when_the_table_is_empty():
    for kind in ClaimKind:
        m.claim_queue_depth.labels(kind.value).set(7)
        m.claim_active_count.labels(kind.value).set(7)

    await phases.sample_claim_gauges_once(_Engine([]))

    for kind in ClaimKind:
        assert (_queued(kind.value), _active(kind.value)) == (0.0, 0.0), kind
