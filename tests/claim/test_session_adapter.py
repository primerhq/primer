import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)


def test_session_adapter_kind():
    a = SessionClaimAdapter(session_storage=None)
    assert a.kind is ClaimKind.SESSION
    assert a.entity_table == "sessions"


def test_session_entity_indexes_back_the_park_queries():
    a = SessionClaimAdapter(session_storage=None)
    ddl = a.entity_indexes('"public"."sessions"')
    joined = "\n".join(ddl)
    # All idempotent and scoped to the qualified table.
    assert all(d.startswith("CREATE INDEX IF NOT EXISTS") for d in ddl)
    assert all('"public"."sessions"' in d for d in ddl)
    # Backs the claim-eligibility filter + listener primary lookups.
    assert "(data->>'parked_status')" in joined
    assert "(data->>'parked_event_key')" in joined
    # GIN backs the multi-event membership fallback (Op.CONTAINS -> ?).
    assert "gin" in joined.lower()
    assert "(data->'parked_event_keys')" in joined


def test_base_adapter_entity_indexes_default_empty():
    # A non-overriding adapter declares no indexes.
    from primer.claim.adapters.chats import ChatClaimAdapter

    a = ChatClaimAdapter(chat_storage=None)
    assert a.entity_indexes('"public"."chats"') == []


def test_session_eligibility_sql():
    a = SessionClaimAdapter(session_storage=None)
    sql = a.eligibility_sql()
    # parked_status lives in the JSONB ``data`` column; a bare ``e.parked_status``
    # reference raises UndefinedColumnError on Postgres and breaks the claim loop.
    assert "e.data->>'parked_status'" in sql
    assert "e.parked_status" not in sql
    # Admits unparked (IS NULL) and resumable rows; excludes plain 'parked'.
    assert "IS NULL" in sql
    assert "'resumable'" in sql
    assert "= 'parked'" not in sql


# ---------------------------------------------------------------------------
# Helpers for on_release tests
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_session(session_id: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_no=0,
    )


class FakeStorage:
    def __init__(self, session: WorkspaceSession) -> None:
        self._session = session
        self.updated: list[WorkspaceSession] = []

    async def get(self, id: str, *, conn=None) -> WorkspaceSession | None:
        return self._session if self._session.id == id else None

    async def update(self, entity: WorkspaceSession, *, conn=None) -> WorkspaceSession:
        self.updated.append(entity)
        self._session = entity
        return entity


class FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = defaultdict(bytes)

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self._data[(session_id, "messages.jsonl")] += line

    def read_lines(self, session_id: str, filename: str) -> list[str]:
        raw = self._data.get((session_id, filename), b"")
        return [ln for ln in raw.decode().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# on_release tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_writes_terminal_record_on_reclaim() -> None:
    """A reclaim failure writes an error-kind record to messages.jsonl."""
    sess = _make_session("s1")
    fake_storage = FakeStorage(sess)
    fake_io = FakeWorkspaceIO()

    adapter = SessionClaimAdapter(session_storage=fake_storage, workspace_io=fake_io)
    await adapter.on_release(
        conn=None,
        entity_id="s1",
        outcome=ReleaseOutcome(success=False, last_error="reclaim", drop_lease=True),
    )

    lines = fake_io.read_lines("s1", "messages.jsonl")
    assert lines, "Expected at least one record in messages.jsonl"
    assert any(json.loads(ln)["kind"] == "error" for ln in lines)


@pytest.mark.asyncio
async def test_on_release_writes_error_record_on_generic_failure() -> None:
    """Any failure outcome writes an error-kind record."""
    sess = _make_session("s2")
    fake_storage = FakeStorage(sess)
    fake_io = FakeWorkspaceIO()

    adapter = SessionClaimAdapter(session_storage=fake_storage, workspace_io=fake_io)
    await adapter.on_release(
        conn=None,
        entity_id="s2",
        outcome=ReleaseOutcome(success=False, last_error="worker_crash"),
    )

    lines = fake_io.read_lines("s2", "messages.jsonl")
    assert lines, "Expected at least one record in messages.jsonl"
    record = json.loads(lines[0])
    assert record["kind"] == "error"
    assert record["payload"]["reason"] == "worker_crash"


@pytest.mark.asyncio
async def test_on_release_no_record_on_success() -> None:
    """A successful release does NOT write any message record."""
    sess = _make_session("s3")
    fake_storage = FakeStorage(sess)
    fake_io = FakeWorkspaceIO()

    adapter = SessionClaimAdapter(session_storage=fake_storage, workspace_io=fake_io)
    await adapter.on_release(
        conn=None,
        entity_id="s3",
        outcome=ReleaseOutcome(success=True),
    )

    lines = fake_io.read_lines("s3", "messages.jsonl")
    assert lines == [], "No records expected on successful release"


@pytest.mark.asyncio
async def test_on_release_no_workspace_io_still_updates_storage() -> None:
    """When workspace_io is None, storage is still updated (graceful degradation)."""
    sess = _make_session("s4")
    fake_storage = FakeStorage(sess)

    adapter = SessionClaimAdapter(session_storage=fake_storage, workspace_io=None)
    # Should NOT raise even without workspace_io
    await adapter.on_release(
        conn=None,
        entity_id="s4",
        outcome=ReleaseOutcome(success=False, last_error="reclaim", drop_lease=True),
    )
    # Storage update still happened
    assert len(fake_storage.updated) == 1


@pytest.mark.asyncio
async def test_on_release_success_bumps_turn_no_and_stamps_last_turn_at() -> None:
    """A successful release bumps turn_no and stamps last_turn_at."""
    sess = _make_session("s5")
    fake_storage = FakeStorage(sess)
    adapter = SessionClaimAdapter(session_storage=fake_storage)
    await adapter.on_release(
        conn=None,
        entity_id="s5",
        outcome=ReleaseOutcome(success=True),
    )
    assert len(fake_storage.updated) == 1
    updated = fake_storage.updated[0]
    assert updated.turn_no == 1
    assert updated.last_turn_at is not None
    assert updated.last_worker_id is None


@pytest.mark.asyncio
async def test_on_release_failure_does_not_bump_turn_no() -> None:
    """A failed release MUST NOT bump turn_no or stamp last_turn_at.

    Pre-fix this adapter unconditionally bumped turn_no on every release,
    producing the diagnostic-report symptom: turn_no=1 with last_turn_at=null
    on a session that never actually ran a turn.
    """
    sess = _make_session("s6")
    fake_storage = FakeStorage(sess)
    adapter = SessionClaimAdapter(session_storage=fake_storage)
    await adapter.on_release(
        conn=None,
        entity_id="s6",
        outcome=ReleaseOutcome(success=False, last_error="executor_error"),
    )
    assert len(fake_storage.updated) == 1
    updated = fake_storage.updated[0]
    assert updated.turn_no == 0, "turn_no must stay at its pre-release value on failure"
    assert updated.last_turn_at is None, "last_turn_at must NOT be stamped on failure"
    # Park / worker fields still cleared — that's bookkeeping, not turn accounting.
    assert updated.last_worker_id is None
