"""Fakes for driving ``_stream_tap`` directly: a router with pushable ticks and deltas, a session store that counts, a log IO."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.tap.selector import TapSelector
from tests._support.segmented_io import ListsFiles


class Sub:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.q.get()

    async def aclose(self):
        pass


class Router:
    def __init__(self):
        self.tick = Sub()
        self.delta = Sub()

    def subscribe(self, wid):
        return self.tick

    def subscribe_delta(self, wid):
        return self.delta


class Sess:
    def __init__(self, sid, last_seq=0, turn_status="idle", last_activity_at=None):
        self.id = sid
        self.workspace_id = "w"
        self.last_seq = last_seq
        self.binding = None
        self.turn_status = turn_status
        self.last_activity_at = last_activity_at or datetime(2026, 1, 1, tzinfo=timezone.utc)


class Store:
    """Session store double: ``find`` counts, ``get`` returns the current row."""

    def __init__(self, rows):
        self.rows = {r.id: r for r in rows}
        self.finds = 0
        self.gets = 0

    async def find(self, pred, page):
        self.finds += 1

        class R:
            pass

        r = R()
        rows = list(self.rows.values()) if page.offset == 0 else []
        right = getattr(pred, "right", None)
        if getattr(getattr(right, "left", None), "name", None) == "id":  # the single-session form: (scope AND id == sid)
            rows = [x for x in rows if x.id == right.right.value]
        r.items = rows
        return r

    async def get(self, sid):
        self.gets += 1
        return self.rows[sid]


def line(seq, kind, ts="2026-06-01T00:00:00+00:00", payload=None):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload or {}, "created_at": ts}) + "\n"


class IO(ListsFiles):
    """``read_file`` of ``<state>/<sid>/...`` returns that session's log; counts reads."""

    state_path = ".state"

    def __init__(self, logs: dict[str, bytes]):
        self.logs = logs
        self.reads = 0

    def _log_files(self):
        return {f".state/sessions/{sid}/messages.jsonl": data for sid, data in self.logs.items()}

    async def read_file(self, path):
        self.reads += 1
        return self.logs[path.split("/")[2]]


def pin_id(sid) -> TapSelector:
    return TapSelector(sessions=Predicate(left=FieldRef(name="id"), op=Op.EQ, right=Value(value=sid)))


def seqs_of(frames):
    return [json.loads(f.split("data: ", 1)[1])["seq"] for f in frames if "data: " in f and not f.startswith(": ")]
