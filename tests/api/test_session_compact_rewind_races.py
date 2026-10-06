"""Reproductions of the compact and rewind check-then-act races (PR-5b of the S2a decomposition). TEST-ONLY: no production change.

``compact_session_endpoint`` and ``rewind_session`` refuse a non-idle or parked session up front, then do I/O that takes time
(the summariser call, a workspace read) and finish with a WHOLE-ROW ``sessions.update(row.model_copy(update={"last_seq": ...}))``
of a row read BEFORE that I/O, and a marker whose seq is seeded from that same stale ``last_seq``. A steer that lands in the
window, runs a turn and parks leaves, on ``main`` today:

* the HTTP answer is 200 (the guard ran once, before the window);
* the park is ERASED (``parked_status``/``parked_state`` back to None: the human's question is never answered, the session
  rests WAITING with nothing to resume);
* ``last_seq`` goes BACKWARDS (5 -> 4 in the compact probe);
* the marker reuses the steer's seq (a duplicate seq in ``messages.jsonl``), and the next writer, seeded from the regressed
  ``last_seq``, mints the next duplicate.

Each reproduction has a plain SCENARIO test (the injected steer and park really landed in the window and the route really
answered; so a broken harness cannot hide behind an expected failure) and a strict-xfail BEHAVIOUR test, restricted to
``AssertionError``, asserting only the invariants any correct fix must hold whichever way it answers (409 or not): the park
is intact, the log's seqs are unique, ``last_seq`` is the log's highest seq. The fix (PR-5b: reserve the seq with a guarded
``patch_if`` under ``session_lifecycle_lock``) must delete the markers.
"""

from __future__ import annotations

import json

import pytest

from primer.model.workspace_session import SessionStatus, WorkspaceSession

from tests.api.test_session_compact import _journey, _msg, _StubLLM
from tests.api.test_session_compact import _rec as _compact_rec
from tests.api.test_session_rewind import _LOG as _REWIND_LOG
from tests.api.test_session_rewind import _FakeWorkspace as _RewindWorkspace
from tests.api.test_session_rewind import _rec as _rewind_rec
from tests.api.test_session_rewind import _seed as _rewind_seed
from tests.api.test_session_rewind import _wire as _rewind_wire

_PARK = {
    "status": SessionStatus.RUNNING, "turn_status": "idle", "parked_status": "parked",
    "parked_event_key": "ask_user:s:tc", "parked_state": {"kind": "ask_user"},
}


def _seqs(ws, path: str) -> list[int]:
    records = [json.loads(line) for line in ws._files[path].decode().splitlines()]
    return [r["seq"] for r in records if "seq" in r]


def _broken_invariants(seen: dict, what: str) -> list[str]:
    """What a correct fix must hold whichever way the route answers (409 or not), as a list so a failure shows all of them."""
    row, seqs = seen["row"], seen["seqs"]
    problems = []
    if row.parked_status != "parked" or row.parked_state != {"kind": "ask_user"}:
        problems.append(f"{what} (HTTP {seen['status_code']}) erased the park: parked_status={row.parked_status!r}, parked_state={row.parked_state!r}")
    if len(seqs) != len(set(seqs)):
        problems.append(f"a seq repeats in the log: {seqs}")
    if row.last_seq != max(seqs):
        problems.append(f"last_seq {row.last_seq} is not the log's highest seq {max(seqs)}")
    return problems


async def _row(fake_storage_provider, sid: str) -> WorkspaceSession:
    row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# compact: a steer + park commit while the summariser runs
# ---------------------------------------------------------------------------


async def _compact_with_a_park_committed_during_the_summariser(client, app, fake_storage_provider, monkeypatch) -> dict:
    holder: dict = {}
    sessions = fake_storage_provider.get_storage(WorkspaceSession)

    class _ParkingLLM(_StubLLM):
        async def _stream_impl(self):
            # what a steer that ran a turn and PARKED while the summariser worked leaves behind
            path = ".state/sessions/j-1/messages.jsonl"
            holder["ws"]._files[path] += (
                _compact_rec(4, "user_input", text="STEER") + "\n" + _msg("user", "STEER") + "\n"
                + _compact_rec(5, "yielded", tool="ask_user") + "\n"
            ).encode()
            row = await sessions.get("j-1")
            await sessions.update(row.model_copy(update={"last_seq": 5, **_PARK}))
            async for event in super()._stream_impl():
                yield event

    ws, _ = await _journey(app, fake_storage_provider, monkeypatch, llm=_ParkingLLM("the story so far"))
    holder["ws"] = ws
    response = await client.post("/v1/workspaces/ws-1/sessions/j-1/compact")
    return {
        "status_code": response.status_code,
        "row": await _row(fake_storage_provider, "j-1"),
        "seqs": _seqs(ws, ".state/sessions/j-1/messages.jsonl"),
    }


@pytest.mark.asyncio
async def test_scenario_a_steer_and_park_landed_in_the_compaction_window(client, app, fake_storage_provider, monkeypatch):
    seen = await _compact_with_a_park_committed_during_the_summariser(client, app, fake_storage_provider, monkeypatch)

    assert {4, 5} <= set(seen["seqs"]), "the injected steer and park records are not in the log"
    assert seen["status_code"] in (200, 409), f"the route answered {seen['status_code']}"


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="PR-5b: compact's whole-row write erases a park that committed while the summariser ran and reuses the steer's seq",
)
@pytest.mark.asyncio
async def test_a_park_committed_during_the_summariser_survives_compact_and_no_seq_repeats(
    client, app, fake_storage_provider, monkeypatch,
):
    seen = await _compact_with_a_park_committed_during_the_summariser(client, app, fake_storage_provider, monkeypatch)

    assert not (problems := _broken_invariants(seen, "compact")), problems


# ---------------------------------------------------------------------------
# rewind: a steer + park commit between the guard and the write (during the workspace read)
# ---------------------------------------------------------------------------


async def _rewind_with_a_park_committed_during_the_log_read(client, app, fake_storage_provider) -> dict:
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    path = ".state/sessions/s-1/messages.jsonl"

    class _SteeredWorkspace(_RewindWorkspace):
        async def read_file(self, p: str) -> bytes:
            content = await super().read_file(p)  # the route sees the log as it was...
            if p == path and not getattr(self, "_steered", False):
                self._steered = True              # ...and a steer that parks lands right after it
                self._files[path] += (
                    _rewind_rec(6, "user_input", text="STEER") + "\n" + _rewind_rec(7, "yielded", tool="ask_user") + "\n"
                ).encode()
                row = await sessions.get("s-1")
                await sessions.update(row.model_copy(update={"last_seq": 7, **_PARK}))
            return content

    await _rewind_seed(fake_storage_provider, "s-1")
    ws = _SteeredWorkspace()
    ws.write(path, _REWIND_LOG)
    _rewind_wire(app, ws)
    response = await client.post("/v1/workspaces/ws-1/sessions/s-1/rewind", json={"to_seq": 1})
    return {
        "status_code": response.status_code,
        "row": await _row(fake_storage_provider, "s-1"),
        "seqs": _seqs(ws, path),
    }


@pytest.mark.asyncio
async def test_scenario_a_steer_and_park_landed_in_the_rewind_window(client, app, fake_storage_provider):
    seen = await _rewind_with_a_park_committed_during_the_log_read(client, app, fake_storage_provider)

    assert {6, 7} <= set(seen["seqs"]), "the injected steer and park records are not in the log"
    assert seen["status_code"] in (200, 409), f"the route answered {seen['status_code']}"


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="PR-5b: rewind's whole-row write erases a park that committed after its guard and reuses the steer's seq",
)
@pytest.mark.asyncio
async def test_a_park_committed_during_the_rewind_survives_it_and_no_seq_repeats(client, app, fake_storage_provider):
    seen = await _rewind_with_a_park_committed_during_the_log_read(client, app, fake_storage_provider)

    assert not (problems := _broken_invariants(seen, "rewind")), problems
