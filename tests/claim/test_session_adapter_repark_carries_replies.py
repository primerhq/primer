"""The session adapter's park branch and the replies a resume did not read (GR-3, Task 103).

Characterization: a park with no ``drained_reply_keys`` (every first park, every agent park) writes its ``parked_state`` whole, as it always
did, whatever replies the row holds.

GR-3: a graph re-park carries the replies the resume did not read and the new park still waits on, makes the row ``resumable``, and never
carries a reply the resume read, nor one for a key the new park no longer waits on. A reply that lands between the release's read and its
write is not erased either.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.int.claim import ParkRequest, ReleaseOutcome
from primer.model.workspace_session import GraphSessionBinding, SessionStatus, WorkspaceSession
from primer.session.yields import durably_mark_session_resumable
from tests.conftest import _InMemoryStorage

pytestmark = pytest.mark.asyncio

SID = "s-carry"
A, B, C = (f"tool_approval:{SID}:{n}:tc-{n}" for n in "abc")
LEAF = {A: "a:tc-a", B: "b:tc-b", C: "c:tc-c"}


def _entry(key: str, decision: str = "approved") -> dict:
    return {"event_key": key, "payload": {"decision": decision}}


def _row(payloads: dict[str, dict], keys: list[str]) -> WorkspaceSession:
    at = datetime.now(UTC) - timedelta(seconds=5)
    return WorkspaceSession(
        id=SID, workspace_id="ws-1", binding=GraphSessionBinding(graph_id="g"), status=SessionStatus.RUNNING, created_at=at,
        parked_status="resumable", parked_at=at, parked_until=at + timedelta(seconds=3600), parked_event_key=keys[0], parked_event_keys=keys,
        parked_state={"yielded": {"event_key": keys[0]}, "resume_event_payloads": payloads,
                      "resume_event_key": next(iter(payloads.values()))["event_key"] if payloads else None},
    )


def _park(keys: list[str], drained: set[str] | None) -> ReleaseOutcome:
    now = datetime.now(UTC)
    return ReleaseOutcome(success=True, drop_lease=True, park=ParkRequest(
        parked_state={"schema_version": 1, "yielded": {"event_key": keys[0]}, "graph_checkpoint": {"nodes": {}}},
        parked_event_key=keys[0], parked_event_keys=keys, parked_until=now + timedelta(seconds=3600), parked_at=now,
        drained_reply_keys=None if drained is None else frozenset(drained),
    ))


async def _release(storage, outcome: ReleaseOutcome) -> WorkspaceSession:
    await SessionClaimAdapter(session_storage=storage).on_release(conn=None, entity_id=SID, outcome=outcome)
    row = await storage.get(SID)
    assert row is not None
    return row


def _held(row: WorkspaceSession) -> list[str]:
    return sorted(e["event_key"] for e in ((row.parked_state or {}).get("resume_event_payloads") or {}).values())


# ---- characterization ------------------------------------------------------------------------------------------------------------------------


async def test_a_park_without_drained_keys_writes_its_state_whole() -> None:
    storage = _InMemoryStorage(WorkspaceSession)
    await storage.create(_row({LEAF[A]: _entry(A), LEAF[B]: _entry(B)}, [A, B]))

    row = await _release(storage, _park([B], drained=None))

    assert (row.parked_status, row.parked_event_key, row.parked_event_keys) == ("parked", B, [B])
    assert row.parked_state == {"schema_version": 1, "yielded": {"event_key": B}, "graph_checkpoint": {"nodes": {}}}


async def test_a_single_event_park_writes_its_columns_as_before() -> None:
    storage = _InMemoryStorage(WorkspaceSession)
    at = datetime.now(UTC)
    await storage.create(WorkspaceSession(id=SID, workspace_id="ws-1", binding=GraphSessionBinding(graph_id="g"), status=SessionStatus.RUNNING,
                                          created_at=at, parked_status="resumable", parked_at=at, parked_event_key=A,
                                          parked_state={"resume_event_payload": {"decision": "approved"}, "resume_event_key": A}))
    park = ParkRequest(parked_state={"yielded": {"event_key": "timer:x"}}, parked_event_key="timer:x", parked_until=at + timedelta(seconds=30),
                       parked_at=at)

    row = await _release(storage, ReleaseOutcome(success=True, drop_lease=True, park=park))

    assert (row.parked_status, row.parked_event_key, row.parked_event_keys, row.parked_until) == ("parked", "timer:x", None, at + timedelta(seconds=30))
    assert row.parked_state == {"yielded": {"event_key": "timer:x"}}


# ---- GR-3 ------------------------------------------------------------------------------------------------------------------------------------


async def test_a_reply_the_resume_did_not_read_is_carried_and_the_row_is_resumable() -> None:
    storage = _InMemoryStorage(WorkspaceSession)
    await storage.create(_row({LEAF[A]: _entry(A), LEAF[B]: _entry(B)}, [A, B, C]))

    row = await _release(storage, _park([B, C], drained={LEAF[A]}))

    assert row.parked_status == "resumable"
    assert (row.parked_event_key, row.parked_event_keys) == (B, [B, C])
    assert _held(row) == [B]
    assert (row.parked_state["resume_event_key"], row.parked_state["resume_event_payload"]) == (B, {"decision": "approved"})
    assert row.parked_state["graph_checkpoint"] == {"nodes": {}}


async def test_a_reply_the_resume_read_is_not_carried_even_when_the_new_park_reuses_its_key() -> None:
    storage = _InMemoryStorage(WorkspaceSession)
    await storage.create(_row({LEAF[A]: _entry(A)}, [A, B]))

    row = await _release(storage, _park([A, B], drained={LEAF[A]}))

    assert (row.parked_status, _held(row)) == ("parked", [])


async def test_a_reply_for_a_key_the_new_park_no_longer_waits_on_is_not_carried() -> None:
    storage = _InMemoryStorage(WorkspaceSession)
    await storage.create(_row({LEAF[A]: _entry(A), LEAF[C]: _entry(C)}, [A, B, C]))

    row = await _release(storage, _park([B], drained={LEAF[A]}))

    assert (row.parked_status, _held(row)) == ("parked", [])


class _ReplyUnderTheRelease(_InMemoryStorage):
    """B's reply lands after the release read the row and before its first write."""

    def __init__(self) -> None:
        super().__init__(WorkspaceSession)
        self.landed = False

    async def patch_if(self, id, patch=None, *, where, set_paths=None, conn=None):
        if not self.landed and ("parked_state", "resume_event_key") in where:
            self.landed = True
            assert await durably_mark_session_resumable(
                await self.get(SID), event_key=B, payload={"decision": "approved"}, session_storage=self, engine=None,
            )
        return await super().patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)


async def test_a_reply_that_lands_between_the_releases_read_and_write_is_carried() -> None:
    storage = _ReplyUnderTheRelease()
    await storage.create(_row({LEAF[A]: _entry(A)}, [A, B]))

    row = await _release(storage, _park([B], drained={LEAF[A]}))

    assert storage.landed
    assert (row.parked_status, _held(row)) == ("resumable", [B])
