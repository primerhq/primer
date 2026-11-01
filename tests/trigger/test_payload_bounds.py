"""Payload templates render bounded and off the event loop (W24, ruling D8: 10 s, 1 MiB, a repetition bound)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from primer.model.principal import PrincipalRef
from primer.model.trigger import DelayedTriggerConfig, SessionAppendSubConfig, Subscription, Trigger
from primer.trigger import dispatch as dispatch_mod
from primer.trigger.dispatch import fire_trigger
from primer.trigger.payload import PayloadTemplateError, render_payload
from primer.trigger.subscribers import DispatchDeps


# --- characterization: renders and errors are what they were ---


def test_ordinary_templates_render_byte_identically() -> None:
    ctx = {"ctx": {"b": 2, "a": 1}, "name": "x", "n": 2}
    assert render_payload(
        "{{ '-' * 3 }}|{{ 3 * '-' }}|{{ [1, 2] * 2 }}|{{ 3 * 4 }}|{{ 2.5 * 2 }}|{{ 2 ** 10 }}|{{ 2 ** -1 }}|{{ 2.0 ** 0.5 }}", ctx,
    ) == "---|---|[1, 2, 1, 2]|12|5.0|1024|0.5|1.4142135623730951"
    assert render_payload(
        "{% for k, v in ctx|dictsort %}{{ k }}={{ v }};{% endfor %}\n{{ ctx | tojson }}  {{ name | upper }}{% if n > 1 %} many{% endif %}\n", ctx,
    ) == 'a=1;b=2;\n{"b": 2, "a": 1}  X many'
    assert render_payload(None, {"b": 1, "a": "x"}) == '{"a": "x", "b": 1}'


@pytest.mark.parametrize(("template", "message"), [
    ("{{ nope }}", "'nope' is undefined"),
    ("{% for %}", "Expected an expression, got 'end of statement block'"),
    ("{{ ''.__class__ }}", "access to attribute '__class__' of 'str' object is unsafe."),
    ("{{ 'a' * 'b' }}", "can't multiply sequence by non-int of type 'str'"),
    ("{{ 1 / 0 }}", "division by zero"),
])
def test_template_errors_keep_their_message(template: str, message: str) -> None:
    with pytest.raises(PayloadTemplateError) as exc:
        render_payload(template, {})
    assert exc.value.message == message


# --- W24: the bounds ---


@pytest.mark.parametrize("template", [
    "{{ 'x' * 2000000 }}",
    "{{ 2000000 * 'x' }}",
    "{{ ['x'] * 2000000 }}",
    "{{ 10 ** 10000000 }}",
])
def test_a_huge_repetition_or_power_is_refused(template: str) -> None:
    started = time.monotonic()
    with pytest.raises(PayloadTemplateError, match="limit"):
        render_payload(template, {})
    assert time.monotonic() - started < 1.0


def test_output_past_one_mib_is_refused() -> None:
    assert len(render_payload("{% for i in range(1024) %}{{ 'x' * 1024 }}{% endfor %}", {})) == 1024 * 1024
    with pytest.raises(PayloadTemplateError, match="1048576 character limit"):
        render_payload("{% for i in range(1025) %}{{ 'x' * 1024 }}{% endfor %}", {})


def test_a_long_render_is_stopped_at_the_time_limit(monkeypatch) -> None:
    from primer.common import bounded_template

    monkeypatch.setattr(bounded_template, "RENDER_SECONDS", 0.2)
    started = time.monotonic()
    with pytest.raises(PayloadTemplateError, match="time limit"):
        render_payload("{% for a in range(4000) %}{% for b in range(4000) %}{% endfor %}{% endfor %}", {})
    assert time.monotonic() - started < 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _fire_one(storage, claim_engine, scheduler, template: str):
    await storage.get_storage(Trigger).create(Trigger(
        id="tr-1", slug="tr-x", name="x", description=None,
        config=DelayedTriggerConfig(fire_at=_now()), enabled=True, next_fire_at=_now(), created_at=_now(),
    ))
    await storage.get_storage(Subscription).create(Subscription(
        owner=PrincipalRef.system(), id="sb-1", trigger_id="tr-1",
        config=SessionAppendSubConfig(session_id="cn-1"), payload_template=template, enabled=True, created_at=_now(),
    ))
    deps = DispatchDeps(storage_provider=storage, claim_engine=claim_engine, scheduler=scheduler, workspace_registry=object())
    return await fire_trigger(trigger_id="tr-1", scheduled_for=None, deps=deps)


@pytest.mark.asyncio
async def test_a_failing_template_reports_payload_template_failed(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
) -> None:
    res = await _fire_one(fake_storage_provider, fake_claim_engine, fake_scheduler, "{{ nope }}")
    assert res.results == [{
        "subscription_id": "sb-1", "ok": False, "skipped": False,
        "error_code": "payload_template_failed", "error_message": "'nope' is undefined",
    }]


@pytest.mark.asyncio
async def test_an_oversized_template_fails_the_subscription_not_the_fire(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
) -> None:
    res = await _fire_one(fake_storage_provider, fake_claim_engine, fake_scheduler, "{{ 'x' * 20000000 }}")
    assert [r["error_code"] for r in res.results] == ["payload_template_failed"]
    assert "limit" in res.results[0]["error_message"]


@pytest.mark.asyncio
async def test_the_render_does_not_stop_the_event_loop(
    fake_storage_provider, fake_claim_engine, fake_scheduler, monkeypatch,
) -> None:
    def slow_render(template, fire_context):
        time.sleep(0.5)
        raise PayloadTemplateError("slow")

    monkeypatch.setattr(dispatch_mod, "render_payload", slow_render)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        res = await _fire_one(fake_storage_provider, fake_claim_engine, fake_scheduler, "x")
    finally:
        task.cancel()
    assert res.results[0]["error_message"] == "slow"
    assert ticks >= 10, ticks
