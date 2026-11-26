"""Review 2026-11-23 6.6: the SSE cursor token stays bounded however many sessions a stream sees, and an oversized Last-Event-ID resets cleanly."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone

import primer.api.routers.tap as tap_mod
from primer.tap.cursor import MAX_TOKEN_CHARS, TapCursor
from primer.tap.router import WorkspaceTick
from primer.tap.selector import TapSelector
from tests.tap.sse_fakes import IO, Router, Sess, Store, line

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
SESSIONS = 300


def _token(frame: str) -> str:
    return frame.split("id: ", 1)[1].split("\n", 1)[0]


async def test_a_stream_over_many_active_sessions_keeps_its_token_small(monkeypatch):
    monkeypatch.setattr(tap_mod, "_KEEPALIVE_INTERVAL_S", 5)
    sids = [f"sess-{i:032x}" for i in range(SESSIONS)]
    io = IO({sid: (line(1, "user_input") + line(2, "done")).encode() for sid in sids})
    router = Router()
    gen = tap_mod._stream_tap(
        router=router, sessions_storage=Store([Sess(sid, last_seq=1) for sid in sids]), workspace_io=io, workspace_id="w",
        selector=TapSelector(), cursor=TapCursor(seqs={}, known_as_of=EPOCH), had_cursor=False,
    )
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    tokens = []
    for sid in sids:
        router.tick.q.put_nowait(WorkspaceTick(session_id=sid, seq=2))
        tokens.append(_token(await asyncio.wait_for(nxt, 2)))
        nxt = asyncio.ensure_future(gen.__anext__())
    nxt.cancel()
    await gen.aclose()
    last = TapCursor.decode(tokens[-1])
    assert len(tokens[-1]) < 8 * 1024
    assert len(last.seqs) == tap_mod._CURSOR_MAX_SESSIONS
    assert sids[-1] in last.seqs and sids[0] not in last.seqs, "the newest sessions are the ones kept"


def test_an_oversized_token_decodes_to_an_empty_cursor_when_capped():
    big = TapCursor(seqs={f"s{i}": i for i in range(5000)}, known_as_of=datetime(2026, 1, 1, tzinfo=timezone.utc))
    token = big.encode()
    assert len(token) > MAX_TOKEN_CHARS
    assert TapCursor.decode(token, max_chars=MAX_TOKEN_CHARS).seqs == {}
    assert len(TapCursor.decode(token).seqs) == 5000, "the MCP drain's token is not capped"


def test_garbled_tokens_reset_cleanly():
    for bad in ["!!!", "a" * 100_000, base64.urlsafe_b64encode(b'{"seqs": [1], "known_as_of": 3}').decode(),
                base64.urlsafe_b64encode(json.dumps({"seqs": {"a": "x"}, "known_as_of": "2026-01-01T00:00:00+00:00"}).encode()).decode()]:
        cursor = TapCursor.decode(bad, max_chars=MAX_TOKEN_CHARS)
        assert cursor.seqs == {} and cursor.known_as_of == EPOCH


def test_encode_keep_writes_only_the_named_sessions():
    cursor = TapCursor(seqs={"a": 1, "b": 2, "c": 3}, known_as_of=EPOCH, offsets={"a": 10, "c": 30})
    kept = TapCursor.decode(cursor.encode(keep=["a", "c"]))
    assert kept.seqs == {"a": 1, "c": 3} and kept.offsets == {"a": 10, "c": 30}
    assert TapCursor.decode(cursor.encode()).seqs == {"a": 1, "b": 2, "c": 3}


def test_encode_keep_on_a_large_map_is_byte_identical_and_keeps_only_the_named():
    """A map with 500 sessions and a 4-entry keep: the token carries only the kept entries and is byte-identical
    to the payload the pre-round-2 encoder (which built it by filtering the whole map) produced (review 2026-11-23,
    M-3: the fix iterates ``keep``, not the whole map, without changing the bytes)."""
    seqs = {f"s{i:04d}": i for i in range(500)}
    offsets = {sid: 1000 + i for i, sid in enumerate(sorted(seqs))}
    cursor = TapCursor(seqs=seqs, offsets=offsets, known_as_of=EPOCH)
    keep = ["s0001", "s0123", "s0499", "absent"]
    token = cursor.encode(keep=keep)
    kept = TapCursor.decode(token)
    assert kept.seqs == {k: seqs[k] for k in keep if k in seqs}
    assert kept.offsets == {k: offsets[k] for k in keep if k in offsets}
    assert kept.drained_as_of == EPOCH
    # The old encoder's payload, built the old way (filter the whole map): the bytes must match exactly.
    legacy = {
        "seqs": {k: v for k, v in seqs.items() if k in keep},
        "known_as_of": EPOCH.isoformat(),
        "offsets": {k: v for k, v in offsets.items() if k in keep},
        "drained_as_of": EPOCH.isoformat(),
    }
    raw = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    assert token == base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()
