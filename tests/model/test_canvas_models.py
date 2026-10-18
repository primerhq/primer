"""The session canvas row, the canvas settings row and the record kinds that go with them (pivot P5, Task 5; design 8.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.canvas.catalog import EVENT_KINDS
from primer.canvas.lang import ROOT_ID
from primer.canvas.settings import read_canvas_settings, write_canvas_settings
from primer.model.canvas import (
    CANVAS_EVENT_KINDS,
    CANVAS_SETTINGS_ID,
    MAX_EMIT_DATA_BYTES,
    MAX_EVENT_VALUE_CHARS,
    ROOT_ELEMENT,
    CanvasEvent,
    CanvasSettings,
    SessionCanvas,
)
from primer.model.common import dump_for_storage
from primer.model.workspace_session import SessionMessageKind
from primer.tap.event import TapEventClass


def test_settings_defaults_are_the_designs():
    s = CanvasSettings(id=CANVAS_SETTINGS_ID)
    assert (s.media_policy, s.strict_mode, s.max_elements, s.max_html_bytes, s.max_canvas_bytes, s.max_events,
            s.max_waking_events_per_minute) == ("any-https", False, 200, 65536, 524288, 100, 10)
    assert s.media_allowlist == []


def test_allowlist_hosts_are_validated():
    assert CanvasSettings(id=CANVAS_SETTINGS_ID, media_allowlist=["CDN.Example.com", "*.example.org", "cdn.example.com"]).media_allowlist == ["cdn.example.com", "*.example.org"]
    with pytest.raises(ValidationError):
        CanvasSettings(id=CANVAS_SETTINGS_ID, media_allowlist=["https://x.com"])


@pytest.mark.parametrize("host", ["media.example.com", "*.example.org", "xn--bcher-kva.example", "a-b.c-d.example", "cdn1.example.co.uk", "1.2.3.4"])
def test_a_host_the_allowlist_can_name(host):
    assert CanvasSettings(id=CANVAS_SETTINGS_ID, media_allowlist=[host]).media_allowlist == [host]


@pytest.mark.parametrize("host", [
    "https://x.com", "x", "x..com", ".x.com", "x.com.", "-x.com", "x-.com", "x_y.com", "*.com.*", "a.*.example.com", "*example.com",
    "x.com/path", "x.com:8443", "user@x.com", "", " ", "exa mple.com", "*.", "a" * 64 + ".com", ("a." * 130) + "com",
])
def test_a_bad_allowlist_host_is_refused(host):
    with pytest.raises(ValidationError):
        CanvasSettings(id=CANVAS_SETTINGS_ID, media_allowlist=[host])


def test_the_allowlist_holds_at_most_a_hundred_hosts():
    hosts = [f"h{n}.example.com" for n in range(100)]
    assert len(CanvasSettings(id=CANVAS_SETTINGS_ID, media_allowlist=hosts).media_allowlist) == 100
    with pytest.raises(ValidationError):
        CanvasSettings(id=CANVAS_SETTINGS_ID, media_allowlist=[*hosts, "extra.example.com"])


@pytest.mark.parametrize("field, low, high", [
    ("max_elements", 1, 1000),
    ("max_html_bytes", 1024, 262144),
    ("max_canvas_bytes", 16384, 4194304),
    ("max_events", 10, 1000),
    ("max_waking_events_per_minute", 1, 120),
])
def test_the_limits_are_ranged(field, low, high):
    assert getattr(CanvasSettings(id=CANVAS_SETTINGS_ID, **{field: low}), field) == low
    assert getattr(CanvasSettings(id=CANVAS_SETTINGS_ID, **{field: high}), field) == high
    for outside in (low - 1, high + 1):
        with pytest.raises(ValidationError):
            CanvasSettings(id=CANVAS_SETTINGS_ID, **{field: outside})


def test_settings_round_trip_through_the_storage_dump():
    settings = CanvasSettings(
        id=CANVAS_SETTINGS_ID, media_policy="allowlist", media_allowlist=["media.example.com", "*.example.org"], strict_mode=True,
        max_elements=50, max_html_bytes=2048, max_canvas_bytes=20000, max_events=20, max_waking_events_per_minute=3,
    )
    assert CanvasSettings.model_validate(dump_for_storage(settings)) == settings


def test_the_media_policy_is_one_of_the_three():
    for policy in ("any-https", "allowlist", "none"):
        assert CanvasSettings(id=CANVAS_SETTINGS_ID, media_policy=policy).media_policy == policy
    with pytest.raises(ValidationError):
        CanvasSettings(id=CANVAS_SETTINGS_ID, media_policy="anything")


async def test_read_gives_the_defaults_without_a_row(fake_storage_provider):
    settings = await read_canvas_settings(fake_storage_provider)
    assert settings.media_policy == "any-https" and settings.id == CANVAS_SETTINGS_ID
    assert await fake_storage_provider.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID) is None, "reading never stores the defaults"


async def test_read_gives_the_stored_row(fake_storage_provider):
    await fake_storage_provider.get_storage(CanvasSettings).create(CanvasSettings(id=CANVAS_SETTINGS_ID, max_elements=50, strict_mode=True))
    settings = await read_canvas_settings(fake_storage_provider)
    assert (settings.max_elements, settings.strict_mode) == (50, True)


async def test_write_creates_the_row_and_the_next_write_replaces_it(fake_storage_provider):
    first = await write_canvas_settings(fake_storage_provider, CanvasSettings(id=CANVAS_SETTINGS_ID, max_elements=50))
    assert first.max_elements == 50
    second = await write_canvas_settings(fake_storage_provider, CanvasSettings(id=CANVAS_SETTINGS_ID, max_events=20))
    assert (second.max_elements, second.max_events) == (200, 20), "a write is the whole row, not a merge"
    assert (await read_canvas_settings(fake_storage_provider)).max_events == 20


async def test_write_stores_the_one_row_whatever_id_it_is_given(fake_storage_provider):
    written = await write_canvas_settings(fake_storage_provider, CanvasSettings(id="somebody-elses", strict_mode=True))
    assert written.id == CANVAS_SETTINGS_ID
    assert await fake_storage_provider.get_storage(CanvasSettings).get("somebody-elses") is None
    assert (await read_canvas_settings(fake_storage_provider)).strict_mode is True


def test_a_new_canvas_has_an_empty_root_column():
    a, b = SessionCanvas(id="s1", workspace_id="ws"), SessionCanvas(id="s2", workspace_id="ws")
    assert a.elements == {"root": {"type": "Column", "props": {}, "children": []}}
    assert a.elements["root"] is not b.elements["root"]
    assert a.elements["root"] is not ROOT_ELEMENT, "a canvas never holds the shared constant"
    assert (a.rev, a.event_seq, a.seen_event_seq, a.values, a.value_set_by, a.events, a.wake_times, a.updated_at) == (0, 0, 0, {}, {}, [], [], None)


def test_the_root_element_is_the_one_the_batch_module_uses():
    from primer.canvas import state

    assert state.ROOT_ELEMENT is ROOT_ELEMENT
    assert ROOT_ID == "root" and set(SessionCanvas(id="s", workspace_id="w").elements) == {ROOT_ID}


def test_a_canvas_row_round_trips_through_the_storage_dump():
    canvas = SessionCanvas(
        id="s1", workspace_id="ws", rev=3,
        elements={
            "root": {"type": "Column", "props": {}, "children": ["genre"]},
            "genre": {"type": "Select", "props": {"label": "Genre", "options": ["Jazz", "Lo-fi"]}},
        },
        values={"genre": "Lo-fi", "player": {"playing": True, "position": 42.0}},
        value_set_by={"genre": "user", "player": "user"},
        events=[CanvasEvent(
            id="e1", seq=1, rev=3, element="genre", type="Select", kind="change", source="control", by="alice", wake=False,
            at="2026-10-18T12:00:00Z", value="Lo-fi", label="Genre", client_event_id="c-1",
        )],
        event_seq=1, seen_event_seq=0, wake_times=["2026-10-18T12:00:00Z"], updated_at="2026-10-18T12:00:01Z",
    )
    dumped = dump_for_storage(canvas)
    assert dumped["events"][0]["at"] == "2026-10-18T12:00:00Z" and dumped["wake_times"] == ["2026-10-18T12:00:00Z"]
    assert SessionCanvas.model_validate(dumped) == canvas


def test_a_value_may_only_be_set_by_the_agent_or_the_user():
    with pytest.raises(ValidationError):
        SessionCanvas(id="s1", workspace_id="ws", value_set_by={"genre": "nobody"})


@pytest.mark.parametrize("field", ["rev", "event_seq", "seen_event_seq"])
def test_the_counters_never_go_negative(field):
    with pytest.raises(ValidationError):
        SessionCanvas(id="s1", workspace_id="ws", **{field: -1})


def test_an_event_keeps_what_the_client_sent_apart_from_what_the_server_knows():
    event = CanvasEvent(
        id="e2", seq=2, rev=1, element="viz", type="Html", kind="emit", source="html:viz", by="alice", wake=True,
        at="2026-10-18T12:00:00Z", name="go", data={"n": 1},
    )
    assert (event.value, event.position, event.label, event.client_event_id) == (None, None, None, None)
    assert event.model_dump(mode="json", exclude={"client_event_id"})["name"] == "go"


def test_the_event_kinds_are_the_catalogs():
    """The model cannot import the catalog, so the two lists are pinned to each other here."""
    assert set(CANVAS_EVENT_KINDS) == {kind for kinds in EVENT_KINDS.values() for kind in kinds}
    assert len(set(CANVAS_EVENT_KINDS)) == len(CANVAS_EVENT_KINDS)


def test_the_event_caps_are_the_reports():
    assert (MAX_EVENT_VALUE_CHARS, MAX_EMIT_DATA_BYTES) == (1000, 4096)


def test_the_record_kinds_have_tap_classes():
    assert (SessionMessageKind.CANVAS_OP, SessionMessageKind.CANVAS_EVENT) == ("canvas_op", "canvas_event")
    assert TapEventClass("canvas_op") is TapEventClass.CANVAS_OP and TapEventClass("canvas_event") is TapEventClass.CANVAS_EVENT
