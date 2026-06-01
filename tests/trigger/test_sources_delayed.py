"""Delayed source — Spec §4.1."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.trigger.sources.delayed import DelayedSource
from primer.model.trigger import (
    Trigger, DelayedTriggerConfig,
)


def _make_trigger(fire_at: datetime) -> Trigger:
    return Trigger(
        id="tr-1", slug="one-off",
        name="One-off", description=None,
        config=DelayedTriggerConfig(fire_at=fire_at),
        enabled=True, next_fire_at=fire_at,
        created_at=datetime.now(timezone.utc),
    )


def test_delayed_eligible_for_claim():
    src = DelayedSource()
    assert src.eligible_for_claim is True


def test_delayed_next_fire_at_first_compute():
    src = DelayedSource()
    fa = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    t = _make_trigger(fa)
    now = fa  # arbitrary; not used for first-compute
    # Before any fire, the next fire is just fire_at.
    assert src.compute_next_fire_at(t, now=now) == fa


def test_delayed_next_fire_at_after_fired_is_none():
    src = DelayedSource()
    fa = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    t = _make_trigger(fa)
    t.last_fired_at = fa  # mark as fired
    now = fa
    assert src.compute_next_fire_at(t, now=now) is None


def test_delayed_build_fire_context():
    src = DelayedSource()
    fa = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    t = _make_trigger(fa)
    ctx = src.build_fire_context(t, fired_at=fa, scheduled_for=None)
    assert ctx["kind"] == "delayed"
    assert ctx["trigger_id"] == "tr-1"
    assert ctx["trigger_slug"] == "one-off"
    assert "fired_at" in ctx
