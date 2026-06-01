"""Payload-template rendering + fire_id helpers — Spec §6, §12.6."""

from __future__ import annotations
from datetime import datetime, timezone

import pytest

from primer.trigger.fire_id import make_fire_id
from primer.trigger.payload import (
    render_payload, PayloadTemplateError,
)


def test_make_fire_id_is_deterministic():
    fired_at = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    a = make_fire_id("tr-1", fired_at)
    b = make_fire_id("tr-1", fired_at)
    assert a == b
    assert a.startswith("fire-tr-1-")


def test_make_fire_id_differs_per_trigger():
    fired_at = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    assert make_fire_id("tr-1", fired_at) != make_fire_id("tr-2", fired_at)


def test_render_payload_none_returns_json_of_context():
    ctx = {"trigger_id": "tr-1", "fired_at": "2026-06-01T09:00:00+00:00"}
    out = render_payload(None, ctx)
    assert isinstance(out, str)
    assert "tr-1" in out


def test_render_payload_jinja_works():
    ctx = {"trigger_id": "tr-1", "fired_at": "2026-06-01T09:00:00+00:00"}
    out = render_payload("hello at {{ fired_at }}", ctx)
    assert out == "hello at 2026-06-01T09:00:00+00:00"


def test_render_payload_strict_undefined_raises():
    with pytest.raises(PayloadTemplateError):
        render_payload("{{ does_not_exist }}", {"trigger_id": "tr-1"})


def test_render_payload_sandbox_blocks_dunder():
    with pytest.raises(PayloadTemplateError):
        render_payload("{{ ''.__class__ }}", {"trigger_id": "tr-1"})
