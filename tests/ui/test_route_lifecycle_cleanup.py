"""Cleanup must preserve failures; it must never opt into error suppression."""

from types import SimpleNamespace

import pytest

from tests.ui_e2e._route_lifecycle import draining_page_routes


def test_callback_failure_from_drain_remains_a_failure():
    def unroute_all(*, behavior):
        assert behavior == "wait"
        raise AssertionError("route callback failed")

    with pytest.raises(AssertionError, match="route callback failed"):
        with draining_page_routes(SimpleNamespace(unroute_all=unroute_all)):
            pass


def test_deferred_abort_failure_does_not_skip_callback_drain():
    calls = []

    def abort():
        calls.append("abort")
        raise RuntimeError("abort failed")

    def unroute_all(*, behavior):
        calls.append(behavior)

    with pytest.raises(RuntimeError, match="abort failed"):
        with draining_page_routes(SimpleNamespace(unroute_all=unroute_all)) as pending:
            pending.append(SimpleNamespace(abort=lambda: calls.append("other abort")))
            pending.append(SimpleNamespace(abort=abort))
    assert calls == ["abort", "other abort", "wait"]
