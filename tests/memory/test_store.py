"""The memory entry store (agent memory Task 3): entries are documents under active/, candidates/ and archived/ of the agent's collection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from primer.events.recorder import recorder_for
from primer.knowledge.document_service import DocumentService
from primer.memory.collection import memory_collection_id
from primer.memory.entry import EntryBody, parse_body
from primer.memory.store import MemoryEntry, MemoryStore
from primer.model.collection import Collection
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.memory import ItemEdit, MemoryAdd, MemoryPatch
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio

AGENT = "a1"
CID = memory_collection_id(AGENT)
SRC = {"session_id": "s1", "from_turn": 1, "to_turn": 4}
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    await provider.get_event_store().ensure_schema()
    await provider.get_storage(Collection).create(Collection(id=CID, description="memory", memory_of=AGENT))
    yield provider
    await provider.aclose()


@pytest.fixture
def store(sp) -> MemoryStore:
    return MemoryStore(sp, DocumentService(sp), recorder_for(sp))


def _add(name: str = "Retry GitHub 429", **kw) -> MemoryAdd:
    base = dict(op="add", name=name, description="when a call is rate limited", steps=["wait", "retry"], pitfalls=["do not hammer"], example=None, topics=["api"])
    return MemoryAdd(**{**base, **kw})


async def _candidate(store: MemoryStore, needs_approval: bool = False, name: str = "Retry GitHub 429") -> MemoryEntry:
    entry = await store.add_candidate(AGENT, _add(name), source=SRC, taught_by="owner", needs_approval=needs_approval)
    assert entry is not None
    return entry


async def _types(sp) -> list[str]:
    events = await sp.get_event_store().read_after(0, limit=200, event_type_prefix="memory.")
    return [e.event_type for e in events]


async def test_add_candidate_lands_in_candidates_with_meta(store, sp) -> None:
    entry = await _candidate(store)
    assert (entry.name, entry.status) == ("retry-github-429", "candidates")
    assert entry.description == "when a call is rate limited" and entry.body.steps == [("s1", "wait"), ("s2", "retry")]
    m = entry.meta
    assert (m["helpful"], m["harmful"], m["pinned"], m["needs_approval"], m["taught_by"]) == (0, 0, False, False, "owner")
    assert m["topics"] == ["api"] and m["sources"] == [SRC] and m["revisions"] == [] and m["last_validated_at"] is None and m["created_at"]
    read = await DocumentService(sp).read(collection_id=CID, path="candidates/retry-github-429")
    assert read.document.title == "retry-github-429" and parse_body(read.content) == entry.body
    assert [e.name for e in await store.list_entries(AGENT)] == ["retry-github-429"]
    assert [e.name for e in await store.list_entries(AGENT, "active")] == []


async def test_add_existing_name_in_any_folder_is_dropped(store) -> None:
    """Review Focus 4: an existing name means the model should have patched."""
    first = await _candidate(store)
    assert await store.add_candidate(AGENT, _add(), source=SRC, taught_by="owner", needs_approval=False) is None
    await store.approve(AGENT, first.name, actor="user:o")
    assert await store.add_candidate(AGENT, _add(), source=SRC, taught_by="owner", needs_approval=False) is None
    await store.archive(AGENT, first.name, actor="user:o", reason="r")
    assert await store.add_candidate(AGENT, _add(), source=SRC, taught_by="owner", needs_approval=False) is None
    assert [e.status for e in await store.list_entries(AGENT)] == ["archived"]


async def test_add_unsluggable_name_is_dropped(store) -> None:
    """Review Focus 4: the name is slugified to [a-z0-9-]; nothing left means no entry."""
    assert await store.add_candidate(AGENT, _add("!!!"), source=SRC, taught_by="owner", needs_approval=False) is None
    assert await store.list_entries(AGENT) == []


async def test_add_over_the_body_cap_is_dropped(store) -> None:
    assert await store.add_candidate(AGENT, _add(steps=["x" * 4000]), source=SRC, taught_by="owner", needs_approval=False) is None


def _patch(name: str, text: str) -> MemoryPatch:
    return MemoryPatch(op="patch", name=name, edits=[ItemEdit(action="change", section="steps", item_id="s1", text=text)])


async def test_patch_keeps_last_ten_revisions(store) -> None:
    entry = await _candidate(store)
    for i in range(12):
        patched = await store.patch(AGENT, _patch(entry.name, f"v{i}"), source={"session_id": f"s{i}", "from_turn": 1, "to_turn": 2})
        assert patched is not None
    got = await store.get(AGENT, entry.name)
    assert got.body.steps[0] == ("s1", "v11")
    revisions = got.meta["revisions"]
    assert len(revisions) == 10 and "v10" in revisions[-1]["body"] and "v1" in revisions[0]["body"] and revisions[0]["at"]
    assert len(got.meta["sources"]) >= 2


async def test_patch_of_archived_or_missing_entry_is_skipped_not_resurrected(store, sp) -> None:
    """Review Focus 3: a patch naming an entry the curator just archived or merged away is skipped, not created again."""
    entry = await _candidate(store)
    await store.archive(AGENT, entry.name, actor="memory:a1", reason="merged")
    assert await store.patch(AGENT, _patch(entry.name, "x"), source=SRC) is None
    assert await store.patch(AGENT, _patch("never-existed", "x"), source=SRC) is None
    assert await store.credit(AGENT, entry.name, "helpful", run_succeeded=True, trial=True, at=NOW) is None
    assert await store.credit(AGENT, "never-existed", "helpful", run_succeeded=True, trial=True, at=NOW) is None
    assert [(e.name, e.status) for e in await store.list_entries(AGENT)] == [("retry-github-429", "archived")]
    assert (await store.get(AGENT, entry.name)).body.steps[0] == ("s1", "wait")


async def test_credit_helpful_trial_success_promotes_candidate(store) -> None:
    entry = await _candidate(store)
    got = await store.credit(AGENT, entry.name, "helpful", run_succeeded=True, trial=True, at=NOW)
    assert got.status == "active" and got.meta["helpful"] == 1
    assert (await store.get(AGENT, entry.name)).status == "active"


@pytest.mark.parametrize(("trial", "succeeded"), [(False, True), (True, False)])
async def test_credit_without_trial_success_keeps_the_candidate(store, trial, succeeded) -> None:
    entry = await _candidate(store)
    got = await store.credit(AGENT, entry.name, "helpful", run_succeeded=succeeded, trial=trial, at=NOW)
    assert got.status == "candidates" and got.meta["helpful"] == 1


async def test_credit_does_not_promote_needs_approval(store) -> None:
    entry = await _candidate(store, needs_approval=True)
    got = await store.credit(AGENT, entry.name, "helpful", run_succeeded=True, trial=True, at=NOW)
    assert got.status == "candidates" and got.meta["helpful"] == 1


async def test_credit_harmful_archives_candidate(store) -> None:
    entry = await _candidate(store)
    got = await store.credit(AGENT, entry.name, "harmful", run_succeeded=False, trial=True, at=NOW)
    assert got.status == "archived" and got.meta["harmful"] == 1


async def test_harmful_credit_on_an_active_entry_only_counts(store) -> None:
    entry = await _candidate(store)
    await store.approve(AGENT, entry.name, actor="user:o")
    got = await store.credit(AGENT, entry.name, "harmful", run_succeeded=False, trial=False, at=NOW)
    assert got.status == "active" and got.meta["harmful"] == 1


async def test_helpful_credit_sets_last_validated_at_on_active(store) -> None:
    entry = await _candidate(store)
    await store.approve(AGENT, entry.name, actor="user:o")
    got = await store.credit(AGENT, entry.name, "helpful", run_succeeded=True, trial=False, at=NOW)
    assert got.meta["last_validated_at"] == NOW.isoformat() and got.meta["helpful"] == 1
    later = await store.credit(AGENT, entry.name, "helpful", run_succeeded=True, trial=False, at=NOW + timedelta(days=1))
    assert later.meta["helpful"] == 2 and later.meta["last_validated_at"] == (NOW + timedelta(days=1)).isoformat()


async def test_concurrent_credits_are_not_lost(store) -> None:
    import asyncio

    entry = await _candidate(store)
    await asyncio.gather(*[store.credit(AGENT, entry.name, "helpful", run_succeeded=False, trial=False, at=NOW) for _ in range(6)])
    assert (await store.get(AGENT, entry.name)).meta["helpful"] == 6


async def test_approve_moves_candidate_to_active(store) -> None:
    entry = await _candidate(store, needs_approval=True)
    got = await store.approve(AGENT, entry.name, actor="user:o")
    assert got.status == "active" and got.meta["needs_approval"] is False
    assert (await store.approve(AGENT, entry.name, actor="user:o")).status == "active", "approving an active entry is a no-op"
    with pytest.raises(NotFoundError):
        await store.approve(AGENT, "missing", actor="user:o")
    await store.archive(AGENT, entry.name, actor="user:o", reason="r")
    with pytest.raises(ConflictError):
        await store.approve(AGENT, entry.name, actor="user:o")


async def test_pin_and_edit(store) -> None:
    entry = await _candidate(store)
    with pytest.raises(ConflictError):
        await store.set_pinned(AGENT, entry.name, True, actor="user:o")
    await store.approve(AGENT, entry.name, actor="user:o")
    assert (await store.set_pinned(AGENT, entry.name, True, actor="user:o")).meta["pinned"] is True
    edited = await store.edit(
        AGENT, entry.name, description="new when", body=EntryBody(steps=[("s1", "only this")], pitfalls=[], example=[]), actor="user:o",
    )
    assert edited.description == "new when" and edited.body.steps == [("s1", "only this")] and len(edited.meta["revisions"]) == 1
    assert edited.meta["pinned"] is True and edited.meta["helpful"] == 0, "an edit leaves the other meta alone"


async def test_every_write_emits_its_event(store, sp) -> None:
    entry = await _candidate(store)                                                    # added
    await store.patch(AGENT, _patch(entry.name, "v"), source=SRC)                       # updated
    await store.credit(AGENT, entry.name, "helpful", run_succeeded=True, trial=True, at=NOW)   # updated, promoted
    await store.set_pinned(AGENT, entry.name, True, actor="user:o")                     # updated
    await store.edit(AGENT, entry.name, description="d", actor="user:o")                # updated
    await store.archive(AGENT, entry.name, actor="user:o", reason="r")                  # archived
    second = await _candidate(store, name="second", needs_approval=True)
    await store.approve(AGENT, second.name, actor="user:o")                             # approved
    types = await _types(sp)
    assert types[0] == "memory.entry.added"
    for wanted in ("memory.entry.added", "memory.entry.updated", "memory.entry.promoted", "memory.entry.archived", "memory.entry.approved"):
        assert wanted in types, (wanted, types)
    events = await sp.get_event_store().read_after(0, limit=200, event_type_prefix="memory.")
    assert {(e.entity_kind, e.entity_id) for e in events} == {("agent", AGENT)}
    by_type = {e.event_type: e for e in reversed(events)}     # the first event of each type
    assert by_type["memory.entry.added"].actor == f"memory:{AGENT}" and by_type["memory.entry.added"].payload["name"] == "retry-github-429"
    assert by_type["memory.entry.archived"].actor == "user:o" and by_type["memory.entry.archived"].payload["reason"] == "r"


async def _archived_between_read_and_write(store, entry) -> None:
    """The curator archives the entry after the worker read it and before the worker writes."""
    real_find = store._find
    docs = store._documents

    async def find_then_archive(agent_id, name):
        found = await real_find(agent_id, name)
        if found is not None and found.status == "candidates":
            store._find = real_find
            await docs.move(collection_id=f"mem-{agent_id}", src=f"candidates/{entry.name}", dst=f"archived/{entry.name}")
        return found

    store._find = find_then_archive


@pytest.mark.parametrize("write", ["patch", "edit"])
async def test_a_body_write_does_not_recreate_an_entry_the_curator_just_moved(store, sp, write) -> None:
    entry = await _candidate(store)
    await _archived_between_read_and_write(store, entry)
    if write == "patch":
        assert await store.patch(AGENT, _patch(entry.name, "late"), source=SRC) is None
    else:
        with pytest.raises(NotFoundError):
            await store.edit(AGENT, entry.name, body=entry.body, actor="user:o")
    assert await store.list_entries(AGENT, "candidates") == []
    archived = await store.get(AGENT, entry.name)
    assert archived is not None and archived.status == "archived"
    assert [s for s in archived.body.steps if s[1] == "late"] == []
    assert len(await store.list_entries(AGENT)) == 1
