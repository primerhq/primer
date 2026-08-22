"""S6 P1: session_append is a first-class subscription kind.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 3.
"""

from __future__ import annotations

from pydantic import TypeAdapter

from primer.model.trigger import (
    SessionAppendSubConfig,
    SubscriptionConfig,
    SubscriptionKind,
)


def test_kind_enum_has_session_append():
    assert SubscriptionKind.SESSION_APPEND.value == "session_append"


def test_config_round_trips_through_the_union():
    ta = TypeAdapter(SubscriptionConfig)
    cfg = ta.validate_python(
        {"kind": "session_append", "session_id": "sess-1"}
    )
    assert isinstance(cfg, SessionAppendSubConfig)
    assert cfg.session_id == "sess-1"


def test_session_id_is_required():
    ta = TypeAdapter(SubscriptionConfig)
    try:
        ta.validate_python({"kind": "session_append"})
    except Exception as exc:
        assert "session_id" in str(exc)
    else:  # pragma: no cover - the validate must not succeed
        raise AssertionError("session_id must be required")
