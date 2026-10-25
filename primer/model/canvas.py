"""The agent canvas of a session: its row, its events and the operator's settings (pivot P5, design 8.5).

``SessionCanvas`` is the ONE row per session (``id`` is the session id) that holds what the agent draws and the user touches: the
flat, id-keyed ``elements``, the live ``values`` of the controls and who set each, a ring of the latest ``events``, and the counters
that fence every write. It is the source of truth: nothing is rebuilt from the session log, and a rewind, a compaction, a restart or
a binding switch does not touch it. ``primer.canvas.service`` writes it, only ever with a field-scoped ``patch_if`` fenced on the
counters it read.

* ``rev`` counts content changes: a batch (agent or operator), a clear and a value-changing event (``change``, ``set_value``). A
  click, a play, a pause, an end, a link open or an emit bumps ``event_seq`` instead. An event is fenced on ``rev`` and
  ``event_seq``.
* ``value_seq`` moves with every event that writes ``values`` (a value change, and an audio event, which sets the element's play
  state without moving ``rev``). A batch is fenced on ``rev`` and ``value_seq`` only (the S6 decision on audit G8-04): it writes
  ``elements``, ``values`` and ``value_set_by`` and nothing an event writes besides, so an event that leaves the values alone (a
  click, an emit, a link open, a value set to what it already is) never fails an agent's batch, while one that set a value since the
  batch read the row makes it read again, so no value is lost.
* ``events`` is a ring of at most ``CanvasSettings.max_events``; ``seen_event_seq`` is how far the agent has read it in order, and
  ``seen_ahead`` the events past that point it has already read (a ``canvas_wait`` takes only the events it matches, so an earlier
  one it did not match stays unread; audit G8-07). ``seen_version`` moves with every write of the two and fences them.
* ``client_events`` holds the last :data:`MAX_CLIENT_EVENT_IDS` retry keys a client sent with an event, each with the event's seq, so a
  retried POST is recognised however short the event ring is (review of #908, round 2).
* ``wake_times`` is the ring the waking-events-per-minute limit reads, ``silent_times`` the ring of the cap on the events that do not
  wake the agent (:data:`MAX_SILENT_EVENTS_PER_MINUTE`).

``CanvasSettings`` is the single operator row (``CANVAS_SETTINGS_ID``). This module imports nothing from ``primer.canvas``, which
imports it.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from primer.model.common import Identifiable

#: The id of the one ``CanvasSettings`` row.
CANVAS_SETTINGS_ID = "_canvas_settings"

#: Where a canvas may load external media from: any https host (the default; agents can already reach the web through tools),
#: only the hosts of ``CanvasSettings.media_allowlist`` (click to load), or nowhere.
MediaPolicy = Literal["any-https", "allowlist", "none"]

#: The id of the root element (``primer.canvas.lang.ROOT_ID`` is the same word; a test pins it).
_ROOT_ID = "root"

#: The root element: always present, always a Column. Never mutate it; copy it.
ROOT_ELEMENT: dict[str, Any] = {"type": "Column", "props": {}, "children": []}

#: The most characters of JSON the value of one event may hold.
MAX_EVENT_VALUE_CHARS = 1000

#: The most bytes of JSON the data of one Html ``emit`` may hold.
MAX_EMIT_DATA_BYTES = 4096

#: The most bytes a request body of ``POST .../canvas/events`` may hold, refused (413) before the body is read, so the service never walks
#: a bigger ``value`` or ``data`` (the #790 review, binding for S6). An event holds at most a 1000-character value or 4096 bytes of emit
#: data; even with every character written as JSON escapes (12 bytes for a character outside the BMP) that is under 50 000 bytes.
MAX_EVENT_BODY_BYTES = 65_536

#: The most bytes a request body of ``POST .../canvas/ops`` may hold (413 before it is read): a batch is at most 600 000 characters of
#: source or of ops JSON, 7 200 000 bytes with every character escaped.
MAX_OPS_BODY_BYTES = 8 * 1024 * 1024

#: The most events that do NOT wake the agent one session's canvas accepts in a minute (the lead's ruling 3 on S6, audit G8-04). Only
#: waking events had a limit (``CanvasSettings.max_waking_events_per_minute``), so an Html element calling ``setValue`` in a loop could
#: drive a ring rewrite, an event row and a frame 20 times a second. 120 is two a second on average, well above what a person makes by
#: choosing, playing or opening things; the renderer debounces an Html ``setValue``. Over the cap the event is refused (429
#: ``silent_rate_limited``) and nothing is recorded.
MAX_SILENT_EVENTS_PER_MINUTE = 120

#: How many of a canvas's latest client retry keys (``CanvasEvent.client_event_id``) are kept to recognise a retried POST, whatever
#: ``CanvasSettings.max_events`` keeps of the events themselves.
MAX_CLIENT_EVENT_IDS = 256

#: Every kind of event a canvas element can send (``primer.canvas.catalog.EVENT_KINDS`` says which element sends which).
CANVAS_EVENT_KINDS = ("click", "change", "emit", "set_value", "play", "pause", "ended", "open")

_HOST_RE = re.compile(r"(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
_MAX_HOST_CHARS = 253
_MAX_ALLOWLIST_HOSTS = 100


class CanvasSettings(Identifiable):
    """What an operator can tune about every canvas, stored as one row at ``CANVAS_SETTINGS_ID``."""

    agent_tools_enabled: bool = Field(
        default=True,
        description="Whether the canvas toolset is exposed to agents and the tool catalogue. On by default now that the canvas panel ships (the setting still lets an operator switch the canvas tools off).",
    )
    media_policy: MediaPolicy = Field(
        default="any-https",
        description="Where a canvas may load external images and audio from: any https host, only the allowlist, or nowhere.",
    )
    media_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "Hosts the 'allowlist' media policy loads from, at most 100: a host name or '*.' and a domain, lowercased and "
            "without duplicates."
        ),
    )
    strict_mode: bool = Field(
        default=False,
        description="When on, an Html element stays inert until the user clicks Run; when off, its scripts run at once.",
    )
    max_elements: int = Field(default=200, ge=1, le=1000, description="The most elements a canvas holds (the root not counted).")
    max_html_bytes: int = Field(default=65536, ge=1024, le=262144, description="The most bytes of one Html element's body.")
    max_canvas_bytes: int = Field(default=524288, ge=16384, le=4194304, description="The most bytes of JSON a whole canvas holds.")
    max_events: int = Field(default=100, ge=10, le=1000, description="How many events a canvas keeps for the agent to read.")
    max_waking_events_per_minute: int = Field(
        default=10, ge=1, le=120,
        description="How many events that wake the agent one session accepts in a minute.",
    )

    @field_validator("media_allowlist")
    @classmethod
    def _hosts(cls, hosts: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for host in hosts:
            cleaned = host.strip().lower()
            if len(cleaned) > _MAX_HOST_CHARS or _HOST_RE.fullmatch(cleaned) is None:
                raise ValueError(f"{host[:64]!r} is not a host name (for example media.example.com or *.example.com)")
            seen.setdefault(cleaned, None)
        if len(seen) > _MAX_ALLOWLIST_HOSTS:
            raise ValueError(f"the allowlist holds at most {_MAX_ALLOWLIST_HOSTS} hosts")
        return list(seen)


class CanvasEvent(BaseModel):
    """One thing a user did on a canvas, as the server validated and numbered it.

    ``id`` is ``e<seq>``. ``rev`` is the canvas rev after the event: a value change moved it, a click did not. ``source`` is
    ``control`` for a control the host renders and ``html:<element id>`` for an Html element. ``label`` comes from the element's
    props, never from the request. ``client_event_id`` is the client's retry key; it is kept only to drop a repeated POST and is not
    part of what the platform event, the record or the agent sees.
    """

    id: str
    seq: int
    rev: int
    element: str
    type: str
    kind: str
    source: str
    by: str
    wake: bool
    at: datetime
    value: Any = None
    name: str | None = None
    data: Any = None
    position: float | None = None
    label: str | None = None
    client_event_id: str | None = None

    def public(self) -> dict[str, Any]:
        """The event as the platform event and the record carry it: JSON, without the client's retry key."""
        return self.model_dump(mode="json", exclude={"client_event_id"})


def _new_elements() -> dict[str, dict[str, Any]]:
    return {_ROOT_ID: copy.deepcopy(ROOT_ELEMENT)}


class SessionCanvas(Identifiable):
    """The canvas of one session (``id`` is the session id). See the module docstring."""

    workspace_id: str
    rev: int = Field(default=0, ge=0)
    elements: dict[str, dict[str, Any]] = Field(default_factory=_new_elements)
    values: dict[str, Any] = Field(default_factory=dict)
    value_set_by: dict[str, Literal["agent", "user"]] = Field(default_factory=dict)
    events: list[CanvasEvent] = Field(default_factory=list)
    event_seq: int = Field(default=0, ge=0)
    seen_event_seq: int = Field(default=0, ge=0)
    seen_ahead: list[int] = Field(default_factory=list)
    seen_version: int = Field(default=0, ge=0)
    value_seq: int = Field(default=0, ge=0)
    wake_times: list[datetime] = Field(default_factory=list)
    silent_times: list[datetime] = Field(default_factory=list)
    client_events: list[tuple[str, int]] = Field(default_factory=list)
    updated_at: datetime | None = None

    def unseen_events(self) -> list[CanvasEvent]:
        """The events after ``seen_event_seq`` that are not in ``seen_ahead``, oldest first: what the agent has not read yet."""
        ahead = set(self.seen_ahead)
        return [event for event in self.events if event.seq > self.seen_event_seq and event.seq not in ahead]


__all__ = [
    "CANVAS_EVENT_KINDS",
    "CANVAS_SETTINGS_ID",
    "MAX_EMIT_DATA_BYTES",
    "MAX_CLIENT_EVENT_IDS",
    "MAX_EVENT_BODY_BYTES",
    "MAX_EVENT_VALUE_CHARS",
    "MAX_OPS_BODY_BYTES",
    "MAX_SILENT_EVENTS_PER_MINUTE",
    "ROOT_ELEMENT",
    "CanvasEvent",
    "CanvasSettings",
    "MediaPolicy",
    "SessionCanvas",
]
