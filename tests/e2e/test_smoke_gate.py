"""Smoke test that proves the MATRIX_RUN_E2E gate works.

This is intentionally NOT a real e2e test — it doesn't touch the network.
It exists so the harness can verify, before the first real test is added,
that ``MATRIX_RUN_E2E=1`` actually causes pytest to collect this directory.
Without the env var, ``tests/e2e/conftest.py`` excludes ``test_*.py`` and
this file is invisible.
"""

from __future__ import annotations


def test_gate_is_open() -> None:
    """Trivial assertion — reaching it proves the gate is open."""
    assert True
