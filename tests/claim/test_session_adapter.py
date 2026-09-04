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


def _make_session(session_id: str, *, last_seq: int = 0) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_no=0,
        last_seq=last_seq,
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


class FakeWorkspaceRegistry:
    """01a068ea: the adapter resolves I/O per-session via a registry (a
    workspace's I/O is not a single process-wide value) -- this fake mirrors
    WorkspaceRegistry.get_workspace's shape (one FakeWorkspaceIO per
    workspace_id, lazily created)."""

    def __init__(self) -> None:
        self.workspaces: dict[str, FakeWorkspaceIO] = {}

    async def get_workspace(self, workspace_id: str) -> FakeWorkspaceIO:
        return self.workspaces.setdefault(workspace_id, FakeWorkspaceIO())


class FakeEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


# ---------------------------------------------------------------------------
# on_release tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_release_writes_terminal_record_on_reclaim() -> None:
    """A reclaim failure writes an error-kind record to messages.jsonl."""
    sess = _make_session("s1")
    fake_storage = FakeStorage(sess)
    registry = FakeWorkspaceRegistry()

    adapter = SessionClaimAdapter(session_storage=fake_storage, workspace_registry=registry)
    await adapter.on_release(
        conn=None,
        entity_id="s1",
        outcome=ReleaseOutcome(success=False, last_error="reclaim", drop_lease=True),
    )

    lines = registry.workspaces[sess.workspace_id].read_lines("s1", "messages.jsonl")
    assert lines, "Expected at least one record in messages.jsonl"
    assert any(json.loads(ln)["kind"] == "error" for ln in lines)


@pytest.mark.asyncio
async def test_on_release_writes_error_record_on_generic_failure() -> None:
    """Any failure outcome writes an error-kind record."""
    sess = _make_session("s2")
    fake_storage = FakeStorage(sess)
    registry = FakeWorkspaceRegistry()

    adapter = SessionClaimAdapter(session_storage=fake_storage, workspace_registry=registry)
    await adapter.on_release(
        conn=None,
        entity_id="s2",
        outcome=ReleaseOutcome(success=False, last_error="worker_crash"),
    )

    lines = registry.workspaces[sess.workspace_id].read_lines("s2", "messages.jsonl")
    assert lines, "Expected at least one record in messages.jsonl"
    record = json.loads(lines[0])
    assert record["kind"] == "error"
    assert record["payload"]["reason"] == "worker_crash"


@pytest.mark.asyncio
async def test_on_release_no_record_on_success() -> None:
    """A successful release does NOT write any message record."""
    sess = _make_session("s3")
    fake_storage = FakeStorage(sess)
    registry = FakeWorkspaceRegistry()

    adapter = SessionClaimAdapter(session_storage=fake_storage, workspace_registry=registry)
    await adapter.on_release(
        conn=None,
        entity_id="s3",
        outcome=ReleaseOutcome(success=True),
    )

    assert registry.workspaces == {}, "No workspace should even be resolved on success"


@pytest.mark.asyncio
async def test_on_release_no_workspace_registry_still_updates_storage() -> None:
    """When workspace_registry is None, storage is still updated (graceful degradation)."""
    sess = _make_session("s4")
    fake_storage = FakeStorage(sess)

    adapter = SessionClaimAdapter(session_storage=fake_storage, workspace_registry=None)
    # Should NOT raise even without workspace_registry
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


@pytest.mark.asyncio
async def test_terminal_record_seeds_past_existing_history() -> None:
    """01a068ea: the default start_seq=0 landed the terminal error record at
    seq=1, silently overwriting whatever real message already held that seq
    on a session that errors out after messages exist. Pre-seed a real
    seq-1 line, then trigger a failure release and assert the error record
    landed at seq=2 -- the pre-existing line is untouched."""
    sess = _make_session("s7", last_seq=1)
    fake_storage = FakeStorage(sess)
    registry = FakeWorkspaceRegistry()
    fake_io = await registry.get_workspace(sess.workspace_id)
    await fake_io.append_message_line(
        "s7", (json.dumps({"seq": 1, "kind": "user_input"}) + "\n").encode(),
    )

    adapter = SessionClaimAdapter(session_storage=fake_storage, workspace_registry=registry)
    await adapter.on_release(
        conn=None,
        entity_id="s7",
        outcome=ReleaseOutcome(success=False, last_error="worker_crash"),
    )

    lines = [json.loads(ln) for ln in fake_io.read_lines("s7", "messages.jsonl")]
    assert len(lines) == 2, "the pre-existing seq-1 line must survive, not be overwritten"
    assert lines[0] == {"seq": 1, "kind": "user_input"}
    assert lines[1]["seq"] == 2
    assert lines[1]["kind"] == "error"


@pytest.mark.asyncio
async def test_terminal_record_publishes_tick() -> None:
    """01a068ea: a durable-but-unticked write is invisible to a live client
    until its next poll -- the terminal-error write needs the same tick
    publish every other durable append in this codebase gets."""
    sess = _make_session("s8")
    fake_storage = FakeStorage(sess)
    registry = FakeWorkspaceRegistry()
    event_bus = FakeEventBus()

    adapter = SessionClaimAdapter(
        session_storage=fake_storage, workspace_registry=registry, event_bus=event_bus,
    )
    await adapter.on_release(
        conn=None,
        entity_id="s8",
        outcome=ReleaseOutcome(success=False, last_error="worker_crash"),
    )

    assert len(event_bus.published) == 1
    key, payload = event_bus.published[0]
    assert key == "session:s8:tick"
    assert "seq" in payload


@pytest.mark.asyncio
async def test_terminal_record_no_event_bus_still_writes() -> None:
    """A None event_bus (graceful degradation, mirrors the no-registry
    case) must not block the write itself."""
    sess = _make_session("s9")
    fake_storage = FakeStorage(sess)
    registry = FakeWorkspaceRegistry()

    adapter = SessionClaimAdapter(
        session_storage=fake_storage, workspace_registry=registry, event_bus=None,
    )
    await adapter.on_release(
        conn=None,
        entity_id="s9",
        outcome=ReleaseOutcome(success=False, last_error="worker_crash"),
    )

    lines = registry.workspaces[sess.workspace_id].read_lines("s9", "messages.jsonl")
    assert lines, "the write must still land even without an event bus"
