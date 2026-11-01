"""The curator (agent memory T9): snapshot, merge, archive, cap; never delete."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import primer.memory.curate as curate_mod
from primer.int.claim import ClaimKind, Lease
from primer.knowledge.collection_query import QueryHit
from primer.memory.curate import curate
from primer.memory.entry import slugify_name
from primer.memory.jobs import run_memory_job
from primer.model.agent import Agent
from primer.model.collection import Document
from primer.model.memory import AgentMemory, MemoryAdd, MemoryJob
from tests.memory.test_reflect import AGENT, CID, NOW, FakeLLM, _deps, _setup, llm, sp  # noqa: F401 - fixtures of that module


async def _world(sp, **settings):
    agent, mem, _ = await _setup(sp, **settings)
    return agent, mem


def _add(name: str, description: str = "d") -> MemoryAdd:
    return MemoryAdd(op="add", name=name, description=description, steps=["a step"], pitfalls=["a pitfall"], example=None, topics=[])


async def _entry(deps, name, status="active", *, age_days=0, description=None, **meta):
    """An entry as the store would have it, then its meta set the way time and credits would have left it."""
    entry = await deps.store.add_candidate(
        AGENT, _add(name, description or f"about {name}"), source={"session_id": "s", "from_turn": 1, "to_turn": 2}, taught_by="u-owner",
        needs_approval=False,
    )
    slug = slugify_name(name)
    created = (NOW - timedelta(days=age_days)).isoformat()
    leaves = {"created_at": created, **meta}
    if status == "active":
        await deps.store.approve(AGENT, slug, actor="user:o")
    doc_id = await deps.store._doc_id(AGENT, status if status != "archived" else "active", slug)
    await deps.store._set_meta(doc_id, leaves)
    if status == "archived":
        await deps.store.archive(AGENT, slug, actor="user:o", reason="r")
    return entry


def _ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


async def _statuses(deps) -> dict[str, str]:
    return {e.name: e.status for e in await deps.store.list_entries(AGENT)}


async def _snapshots(sp):
    return [d for d in (await sp.get_content_store().list(CID, prefix="snapshots/"))]


async def test_snapshot_before_any_change(sp, llm) -> None:
    agent, mem = await _world(sp)
    deps = _deps(sp, llm)
    await _entry(deps, "Stale", last_validated_at=_ago(90))
    await _entry(deps, "Fresh", last_validated_at=_ago(1))
    report = await curate(deps, agent, mem, NOW)
    assert report.archived == 1 and report.snapshot_path.startswith("snapshots/")
    [snap] = await _snapshots(sp)
    assert snap.path == report.snapshot_path
    doc = await deps.store._documents.read(collection_id=CID, path=report.snapshot_path)
    saved = json.loads(doc.content)
    by_path = {e["path"]: e for e in saved["entries"]}
    assert set(by_path) == {"active/stale", "active/fresh"}, "the entries as they were BEFORE the change"
    assert by_path["active/stale"]["meta"]["last_validated_at"] == _ago(90) and "steps" in json.dumps(by_path["active/stale"]["body"])
    assert (await _statuses(deps))["stale"] == "archived"


async def test_merges_near_duplicates_into_older(sp, llm, monkeypatch) -> None:
    agent, mem = await _world(sp)
    deps = _deps(sp, llm)
    await _entry(deps, "Older", age_days=20, last_validated_at=_ago(1), description="retry on 429")
    await _entry(deps, "Newer", age_days=2, last_validated_at=_ago(1), description="back off when rate limited")

    async def fake_query(*, query, path_prefix=None, **kw):
        hits = [QueryHit("active/older", "", 1.0), QueryHit("active/newer", "", 0.95), QueryHit("active/older", "", 0.5)]
        return hits, "semantic"

    monkeypatch.setattr(curate_mod, "run_collection_query", fake_query)
    llm.answers = [{"merged": True, "description": "retry on rate limit", "steps": ["wait", "retry"], "pitfalls": ["no loops"], "example": None}]
    report = await curate(deps, agent, mem, NOW)
    assert (report.merged, len(llm.calls)) == (1, 1), "one confirmation call for the one pair"
    older, newer = await deps.store.get(AGENT, "older"), await deps.store.get(AGENT, "newer")
    assert older.status == "active" and older.description == "retry on rate limit" and [t for _, t in older.body.steps] == ["wait", "retry"]
    assert newer.status == "archived" and newer.meta["merged_into"] == "older"


async def test_a_pair_the_judge_keeps_apart_is_left_alone(sp, llm, monkeypatch) -> None:
    agent, mem = await _world(sp)
    deps = _deps(sp, llm)
    await _entry(deps, "One", age_days=5, last_validated_at=_ago(1))
    await _entry(deps, "Two", age_days=3, last_validated_at=_ago(1))

    async def fake_query(**kw):
        return [QueryHit("active/two", "", 0.97)], "semantic"

    monkeypatch.setattr(curate_mod, "run_collection_query", fake_query)
    llm.answers = [{"merged": False, "description": "", "steps": [], "pitfalls": [], "example": None}]
    report = await curate(deps, agent, mem, NOW)
    assert report.merged == 0 and await _statuses(deps) == {"one": "active", "two": "active"}
    assert await _snapshots(sp) == [], "nothing changed, so no snapshot"


async def test_without_semantic_search_only_identical_descriptions_merge(sp, llm, monkeypatch) -> None:
    agent, mem = await _world(sp)
    deps = _deps(sp, llm)
    await _entry(deps, "Old", age_days=9, last_validated_at=_ago(1), description="Retry  on 429.")
    await _entry(deps, "Dup", age_days=3, last_validated_at=_ago(1), description="retry on 429")
    await _entry(deps, "Other", age_days=3, last_validated_at=_ago(1), description="something else")

    async def fulltext(**kw):
        return [], "fulltext"

    monkeypatch.setattr(curate_mod, "run_collection_query", fulltext)
    llm.answers = [{"merged": True, "description": "retry on 429", "steps": ["s"], "pitfalls": [], "example": None}]
    report = await curate(deps, agent, mem, NOW)
    assert report.merged == 1 and (await _statuses(deps)) == {"old": "active", "dup": "archived", "other": "active"}


async def test_stale_actives_archived_pinned_and_trigger_used_kept(sp, llm) -> None:
    agent, mem = await _world(sp, archive_after_days=60)
    deps = _deps(sp, llm)
    await _entry(deps, "Stale", age_days=100, last_validated_at=_ago(70))
    await _entry(deps, "Never", age_days=100)
    await _entry(deps, "Pinned", age_days=100, last_validated_at=_ago(70), pinned=True)
    await _entry(deps, "Trigger", age_days=100, last_validated_at=_ago(70), last_trigger_use_at=_ago(20))
    await _entry(deps, "Fresh", age_days=100, last_validated_at=_ago(5))
    await _entry(deps, "Young", age_days=10)
    report = await curate(deps, agent, mem, NOW)
    assert report.archived == 2
    assert await _statuses(deps) == {
        "stale": "archived", "never": "archived", "pinned": "active", "trigger": "active", "fresh": "active", "young": "active",
    }


async def test_old_candidates_archived(sp, llm) -> None:
    agent, mem = await _world(sp)
    deps = _deps(sp, llm)
    await _entry(deps, "Old unused", "candidates", age_days=40)
    await _entry(deps, "Old credited", "candidates", age_days=40, helpful=1)
    await _entry(deps, "Recent", "candidates", age_days=10)
    await curate(deps, agent, mem, NOW)
    assert await _statuses(deps) == {"old-unused": "archived", "old-credited": "candidates", "recent": "candidates"}


async def test_cap_keeps_pinned_then_highest_score(sp, llm) -> None:
    agent, mem = await _world(sp, max_active=2)
    deps = _deps(sp, llm)
    ok = {"last_validated_at": _ago(1)}
    await _entry(deps, "Pinned low", pinned=True, helpful=0, harmful=3, **ok)
    await _entry(deps, "Best", helpful=6, harmful=1, **ok)
    await _entry(deps, "Middle", helpful=2, **ok)
    await _entry(deps, "Worst", helpful=0, harmful=2, **ok)
    report = await curate(deps, agent, mem, NOW)
    assert report.archived == 2
    assert await _statuses(deps) == {"pinned-low": "active", "best": "active", "middle": "archived", "worst": "archived"}


async def test_no_changes_no_work(sp, llm) -> None:
    agent, mem = await _world(sp)
    deps = _deps(sp, llm)
    await _entry(deps, "Fine", last_validated_at=_ago(1))
    first = await curate(deps, agent, mem, NOW)
    assert (first.merged, first.archived, first.snapshot_path) == (0, 0, None)
    mem = mem.model_copy(update={"last_curated_at": NOW})
    later = await curate(deps, agent, mem, NOW + timedelta(hours=24))
    assert (later.merged, later.archived, later.snapshot_path) == (0, 0, None)
    assert await _snapshots(sp) == [] and llm.calls == []
    assert await sp.get_event_store().read_after(0, limit=50, event_type_prefix="memory.curated") == []


async def test_never_deletes(sp, llm) -> None:
    agent, mem = await _world(sp, max_active=1)
    deps = _deps(sp, llm)
    for name in ("A", "B", "C"):
        await _entry(deps, name, last_validated_at=_ago(1), helpful=1)
    await _entry(deps, "D", "candidates", age_days=90)
    before = {d.path for d in await sp.get_content_store().list(CID)}
    await curate(deps, agent, mem, NOW)
    after = {d.path for d in await sp.get_content_store().list(CID)}
    assert len(await deps.store.list_entries(AGENT)) == 4 and sum(s == "archived" for s in (await _statuses(deps)).values()) == 3
    assert any(p.startswith("snapshots/") for p in after)
    assert {p.split("/", 1)[1] for p in before} <= {p.split("/", 1)[1] for p in after if not p.startswith("snapshots/")}, "every entry is still a document"
    events = await sp.get_event_store().read_after(0, limit=50, event_type_prefix="memory.curated")
    assert [e.event_type for e in events] == ["memory.curated"] and events[0].payload["archived"] == 3


async def test_the_job_requeues_a_day_later_and_remembers_the_run(sp, llm) -> None:
    agent, mem = await _world(sp)
    deps = _deps(sp, llm)
    await sp.get_storage(MemoryJob).create(MemoryJob(
        id=f"curate:{AGENT}", kind="curate", agent_id=AGENT, created_at=NOW, expires_at=NOW + timedelta(hours=1),
    ))
    lease = Lease(ClaimKind.MEMORY, f"curate:{AGENT}", "w1", NOW, NOW + timedelta(minutes=1), 0, None)
    out = await run_memory_job(deps, lease)
    assert out.success and out.requeue_after == timedelta(hours=24) and not out.drop_lease
    stored = await sp.get_storage(AgentMemory).get(AGENT)
    assert stored.last_curated_at == NOW
    job = await sp.get_storage(MemoryJob).get(f"curate:{AGENT}")
    assert job.expires_at > NOW + timedelta(days=1), "the job outlives its week so the daily chain does not die"
    again = await run_memory_job(deps, lease)
    assert again.requeue_after is not None and again.requeue_after <= timedelta(hours=24), "at most one run a day"
    assert llm.calls == []
