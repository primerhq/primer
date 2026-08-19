"""S7 cardinality guard (crosscheck m2): a label-name allowlist.

Every label on every instrument registered on the dedicated registry must
be a reviewed name. session_id above all must never appear: session-scoped
stats come from the derived timeline, not from a metric dimension.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_metrics():
    import primer.observability.metrics as m
    m.reset_for_test()
    yield
    m.reset_for_test()


def test_every_registered_label_is_allowlisted() -> None:
    import primer.observability.metrics as m
    unknown = m.registered_label_names() - m.ALLOWED_LABEL_NAMES
    assert unknown == set(), f"unallowlisted metric labels: {sorted(unknown)}"


def test_session_id_is_never_a_label() -> None:
    import primer.observability.metrics as m
    assert "session_id" not in m.ALLOWED_LABEL_NAMES
    assert "session_id" not in m.registered_label_names()


def test_guard_survives_reset() -> None:
    import primer.observability.metrics as m
    m.reset_for_test()
    assert m.registered_label_names() - m.ALLOWED_LABEL_NAMES == set()


def test_guard_catches_a_new_unallowlisted_label() -> None:
    """The guard must actually fail when someone adds a rogue label."""
    from prometheus_client import Counter
    import primer.observability.metrics as m

    Counter("rogue_total", "d", ["session_id"], registry=m.registry)
    assert "session_id" in m.registered_label_names()
    assert m.registered_label_names() - m.ALLOWED_LABEL_NAMES == {"session_id"}


def test_spec_taxonomy_names_are_all_present() -> None:
    import primer.observability.metrics as m
    for name in (
        "worker", "kind", "status", "binding_ref", "provider_id",
        "profile_id", "direction", "toolset", "tool", "workspace_id",
    ):
        assert name in m.ALLOWED_LABEL_NAMES
