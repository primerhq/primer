"""The session row's launch-time base prompt round-trips (Wiki First W4).

``base_prompt`` is one passive, persisted row field: a normal API-visible
field (not storage-only, not excluded) written exactly once, by the create.
A row document stored before the field existed reads ``None``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.model.workspace_session import AgentSessionBinding, WorkspaceSession
from tests.conftest import _FakeStorageProvider


def _row(**fields) -> WorkspaceSession:
    base = dict(
        id="s-1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status="created",
        created_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
    )
    base.update(fields)
    return WorkspaceSession(**base)


async def test_a_row_built_with_the_base_prompt_persists_and_reads_back_identical():
    sp = _FakeStorageProvider()
    sessions = sp.get_storage(WorkspaceSession)
    base = "ALWAYS: answer the token question with TOKEN-42"
    await sessions.create(_row(base_prompt=base))

    back = await sessions.get("s-1")

    assert back.base_prompt == base
    # The field is a normal API-visible field: it rides the model's own dump.
    assert _row(base_prompt=base).model_dump(mode="json")["base_prompt"] == base


async def test_a_row_document_without_the_key_reads_none():
    """Rows stored before the field existed have no key; the default is None."""
    stored = _row(base_prompt="x").model_dump(mode="json")
    assert "base_prompt" in stored
    del stored["base_prompt"]

    back = WorkspaceSession.model_validate(stored)

    assert back.base_prompt is None


async def test_the_field_is_not_storage_only_and_not_excluded():
    assert "base_prompt" not in WorkspaceSession._storage_only_fields
    assert "base_prompt" in WorkspaceSession.model_fields
    assert "base_prompt" in _row().model_dump(), "it is not exclude=True: readers see it"
