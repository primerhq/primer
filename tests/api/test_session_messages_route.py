"""REST tests for GET /v1/sessions/{id}/messages — the recorded message log.

Mirrors test_node_states_route.py: a fake workspace exposes read_file +
state_path. The headline behaviour is that an ENDED session still returns
its recorded history (unlike the WS, which rejects ended sessions)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest


def _now() -> datetime:
    return datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def write(self, path: str, content: str) -> None:
        self._files[path] = content.encode("utf-8")

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            from primer.model.except_ import NotFoundError
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    # -- WorkspaceIO write surface (used by WorkspaceMessageWriter) ----------
    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = f"{self.state_path}/sessions/{session_id}/messages.jsonl"
        self._files[path] = self._files.get(path, b"") + line

    async def get_session(self, session_id: str):
        return _FakeSlot()


class _FakeSlot:
    async def append_instruction(self, content: str) -> None:
        pass


class _NoopScheduler:
    async def enqueue(self, session_id: str) -> None:
        pass


async def _seed_session(fake_storage_provider, sid: str, status):
    from primer.model.workspace_session import (
        AgentSessionBinding, SessionStatus, WorkspaceSession,
    )
    sess = WorkspaceSession(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status, created_at=_now(), turn_status="idle",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)


@pytest.mark.asyncio
async def test_ended_session_still_returns_history(
    client: httpx.AsyncClient, app, fake_storage_provider,
):
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-ended", SessionStatus.ENDED)
    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/s-ended/messages.jsonl",
        '{"seq":1,"kind":"assistant_token","payload":{"text":"hi"}}\n'
        '{"seq":2,"kind":"done","payload":{}}\n',
    )

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-ended/messages")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [it["seq"] for it in items] == [1, 2]


@pytest.mark.asyncio
async def test_after_seq_filters(client, app, fake_storage_provider):
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-run", SessionStatus.RUNNING)
    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/s-run/messages.jsonl",
        '{"seq":1,"kind":"a"}\n{"seq":2,"kind":"b"}\n{"seq":3,"kind":"c"}\n',
    )

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-run/messages?after_seq=1")
    assert r.status_code == 200, r.text
    assert [it["seq"] for it in r.json()["items"]] == [2, 3]


@pytest.mark.asyncio
async def test_missing_file_is_empty_not_500(client, app, fake_storage_provider):
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-empty", SessionStatus.RUNNING)
    ws = _FakeWorkspace()  # no messages.jsonl written

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-empty/messages")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_unknown_session_404(client, app, fake_storage_provider):
    r = await client.get("/v1/sessions/nope/messages")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 01a04dde-b331 - messages.jsonl dual-write reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_shows_the_instruction_immediately(
    client, app, fake_storage_provider,
):
    """The exact live repro shape that motivated this fix: a session
    created with initial_instructions, its turn genuinely still running
    (no assistant output yet) - GET must show the user's message from
    the very first poll, not just once a modern record for something
    ELSE happens to land. Post-fix (01a04dde-b331 write-side symmetry),
    a real create now writes both shapes; this proves the read side
    serves exactly one copy of the message, not the pre-fix "only the
    legacy line" gap OR a duplicate from naive pass-through."""
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-live", SessionStatus.RUNNING)
    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/s-live/messages.jsonl",
        '{"role":"user","parts":[{"type":"text","text":"go do the thing"}]}\n'
        '{"seq":1,"kind":"user_input","payload":{"text":"go do the thing"}}\n',
    )

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-live/messages")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1, (
        f"expected the legacy/modern pair to dedupe to exactly one "
        f"item, got {items}"
    )
    assert items[0]["kind"] == "user_input"
    assert items[0]["payload"]["text"] == "go do the thing"
    assert items[0]["seq"] == 1


@pytest.mark.asyncio
async def test_old_session_with_legacy_only_instruction_is_not_lost(
    client, app, fake_storage_provider,
):
    """An "old session": created BEFORE the write-side fix, so its
    opening instruction exists ONLY as a legacy {role,parts} line, with
    no modern USER_INPUT counterpart ever written for it (nothing
    back-fills already-persisted history). The filter must synthesize a
    modern-shaped item for it, not drop it - dropping would erase a
    real user message the raw-passthrough behavior never lost."""
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-old", SessionStatus.ENDED)
    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/s-old/messages.jsonl",
        '{"role":"user","parts":[{"type":"text","text":"the original ask"}]}\n'
        '{"seq":1,"kind":"assistant_token","payload":{"text":"done"}}\n'
        '{"seq":2,"kind":"done","payload":{}}\n',
    )

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-old/messages")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    kinds = [it["kind"] for it in items]
    assert "user_input" in kinds, (
        f"the orphaned legacy instruction must be synthesized, not "
        f"dropped: {items}"
    )
    synthesized = next(it for it in items if it["kind"] == "user_input")
    assert synthesized["payload"]["text"] == "the original ask"
    # Never collides with a real seq (those start at 1) and sorts before
    # all real content - it's backfilled history, always the oldest.
    assert synthesized["seq"] <= 0
    assert [it["seq"] for it in items] == sorted(it["seq"] for it in items)
    # The real modern records are untouched.
    assert "assistant_token" in kinds
    assert "done" in kinds


@pytest.mark.asyncio
async def test_after_seq_poll_never_returns_backfilled_legacy_content(
    client, app, fake_storage_provider,
):
    """A synthesized/backfilled item (seq <= 0) must never survive an
    after_seq-filtered poll, at any after_seq >= 0 - it isn't NEW
    content, it's historical, and a live client polling "what's new"
    must not see it repeatedly resurface."""
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-poll", SessionStatus.RUNNING)
    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/s-poll/messages.jsonl",
        '{"role":"user","parts":[{"type":"text","text":"orphaned ask"}]}\n'
        '{"seq":1,"kind":"assistant_token","payload":{"text":"hi"}}\n'
        '{"seq":2,"kind":"done","payload":{}}\n',
    )

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-poll/messages?after_seq=0")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert all(it["kind"] != "user_input" for it in items), items
    assert [it["seq"] for it in items] == [1, 2]


@pytest.mark.asyncio
async def test_non_user_legacy_lines_pass_through_unchanged(
    client, app, fake_storage_provider,
):
    """Scoping decision (01a04dde-b331): only role="user" legacy lines
    are reconciled. Assistant/tool-role legacy lines (e.g. a
    parked-then-resumed tool result, which has no modern counterpart
    either - a separate, not-yet-fixed write-side gap) are passed
    through exactly as the raw file has them - no worse than today's
    behavior, and never silently dropped by a filter that can't prove
    they're actually covered."""
    from primer.model.workspace_session import SessionStatus
    await _seed_session(fake_storage_provider, "s-resumed", SessionStatus.ENDED)
    ws = _FakeWorkspace()
    ws.write(
        ".state/sessions/s-resumed/messages.jsonl",
        '{"role":"user","parts":[{"type":"text","text":"call the tool"}]}\n'
        '{"seq":1,"kind":"user_input","payload":{"text":"call the tool"}}\n'
        '{"seq":2,"kind":"tool_call","payload":{"id":"tc1","name":"x","arguments":{}}}\n'
        '{"role":"tool","parts":[{"type":"tool_result","id":"tc1","output":"42"}]}\n'
        '{"seq":3,"kind":"done","payload":{}}\n',
    )

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-resumed/messages")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    # The user line deduped (has a matching modern USER_INPUT). The
    # orphaned tool-role legacy line passed through unchanged, exactly
    # as the raw file had it - not dropped, not converted.
    assert sum(1 for it in items if it.get("role") == "user") == 0
    assert sum(1 for it in items if it.get("kind") == "user_input") == 1
    tool_lines = [it for it in items if it.get("role") == "tool"]
    assert len(tool_lines) == 1
    assert tool_lines[0]["parts"][0]["output"] == "42"


@pytest.mark.asyncio
async def test_wake_persists_one_user_input_retrievable_via_endpoint(
    client, app, fake_storage_provider,
):
    """A steer/invoke (wake_session) persists exactly one USER_INPUT record,
    retrievable via GET /sessions/{id}/messages — the record the UI session
    adapter maps to a user_message bubble in the transcript."""
    from primer.model.workspace_session import SessionStatus
    from primer.session.enqueue import SessionWakeDeps, wake_session

    await _seed_session(fake_storage_provider, "s-wake", SessionStatus.CREATED)
    ws = _FakeWorkspace()

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    # The unified invoke = steer = resume "send a message" path.
    await wake_session(
        workspace_id="ws-1",
        session_id="s-wake",
        instruction="do the thing",
        deps=SessionWakeDeps(
            storage_provider=fake_storage_provider,
            scheduler=_NoopScheduler(),
            claim_engine=None,
            workspace_registry=app.state.workspace_registry,
            event_bus=None,
        ),
    )

    r = await client.get("/v1/sessions/s-wake/messages")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    user_inputs = [it for it in items if it["kind"] == "user_input"]
    assert len(user_inputs) == 1, f"expected exactly one USER_INPUT, got {items!r}"
    assert user_inputs[0]["payload"]["text"] == "do the thing"
