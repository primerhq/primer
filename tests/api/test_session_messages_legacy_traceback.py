"""ERROR records written before the traceback came out of the envelope
still carry ``extensions.traceback`` on disk. The read path strips it, so
GET /v1/sessions/{sid}/messages (and the turn-log read, which shares the
reader) never serves one, whatever the row's age.
"""

from __future__ import annotations

import json

from primer.api.routers.sessions import _read_workspace_turn_log

_TB = 'Traceback (most recent call last):\n  File "/app/primer/session/dispatch.py", line 1\nValueError: x\n'


class _FakeWorkspace:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read_file(self, _relative_path: str) -> bytes:
        return self._raw


async def _read(rows: list[dict]) -> list[dict]:
    raw = "\n".join(json.dumps(r) for r in rows).encode("utf-8")
    res = await _read_workspace_turn_log(
        workspace=_FakeWorkspace(raw), relative_path="x",
        limit=100, offset=0, since_seq=None,
    )
    return res["items"]


async def test_a_legacy_messages_error_row_is_served_without_its_traceback():
    [item] = await _read([{
        "seq": 1, "kind": "error",
        "payload": {
            "message": "x", "code": "/errors/internal", "title": "ValueError",
            "status": 500,
            "extensions": {"exception_class": "ValueError", "traceback": _TB},
        },
    }])
    assert "traceback" not in item["payload"]["extensions"]
    assert item["payload"]["extensions"]["exception_class"] == "ValueError"
    assert "dispatch.py" not in json.dumps(item)


async def test_a_legacy_turn_log_failed_row_is_served_without_its_traceback():
    [item] = await _read([{
        "seq": 1, "kind": "failed",
        "error": {
            "type": "/errors/internal", "title": "ValueError", "status": 500,
            "detail": "x",
            "extensions": {"exception_class": "ValueError", "traceback": _TB},
        },
    }])
    assert "traceback" not in item["error"]["extensions"]
    assert item["error"]["extensions"]["exception_class"] == "ValueError"


async def test_rows_without_a_traceback_are_untouched():
    rows = [
        {"seq": 1, "kind": "user_input", "payload": {"text": "hi"}},
        {"seq": 2, "kind": "error", "payload": {"message": "m", "extensions": None}},
        {"seq": 3, "kind": "error", "payload": "not-a-dict"},
    ]
    assert await _read(rows) == rows
