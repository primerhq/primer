"""Reflection (agent memory T7): a scripted LLM, a real store over SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from primer.events.recorder import recorder_for
from primer.knowledge.document_service import DocumentService
from primer.memory.collection import memory_collection_id
from primer.memory.jobs import MemoryDeps
from primer.memory.reflect import DEFAULT_REFLECTION_PROMPT, reflect, used_entries
from primer.memory.store import MemoryStore
from primer.model.agent import Agent, AgentModel
from primer.model.chat import ExtendedPart, Message, TextPart, ToolCallPart, ToolResultPart
from primer.model.collection import Collection
from primer.model.memory import AgentMemory, MemoryAdd, MemoryJob
from primer.model.model_profile import ModelProfile
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
AGENT = "a1"
CID = memory_collection_id(AGENT)
OWNER = PrincipalRef(type="user", id="u-owner", display="owner", role="user", source="local", user_id="u-owner")
GUEST = PrincipalRef(type="user", id="u-guest", display="guest", role="user", source="local", user_id="u-guest")


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.answers: list = []

    async def judge_structured(self, *, model, system_prompt, user_message, response_schema):
        self.calls.append({"model": model, "system_prompt": system_prompt, "user_message": user_message, "schema": response_schema})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeRegistry:
    def __init__(self, llm: FakeLLM) -> None:
        self.llm, self.asked = llm, []

    async def get_llm(self, provider_id):
        self.asked.append(provider_id)
        return self.llm


def _add_delta(name="Retry GitHub 429", **kw) -> dict:
    return {
        "op": "add", "name": name, "description": "retry when rate limited", "steps": ["wait", "retry"], "pitfalls": ["do not hammer"],
        "example": None, "topics": ["github"], **kw,
    }


def _run(deltas, ok=True) -> dict:
    return {"run_succeeded": ok, "deltas": deltas}


def _transcript() -> list[Message]:
    return [
        Message(role="user", parts=[TextPart(text="push the branch")]),
        Message(role="assistant", parts=[ToolCallPart(id="t1", name="git__push", arguments={})]),
        Message(role="tool", parts=[ToolResultPart(id="t1", output="429", error=True)]),
        Message(role="user", parts=[TextPart(text="try again later")]),
        Message(role="assistant", parts=[TextPart(text="done")]),
    ]


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    await provider.get_event_store().ensure_schema()
    await provider.get_storage(Collection).create(Collection(id=CID, description="memory", memory_of=AGENT))
    await provider.get_storage(ModelProfile).create(
        ModelProfile(id="p", description="p", provider_id="prov", model_name="m1", context_length=1000)
    )
    yield provider
    await provider.aclose()


async def _setup(sp, *, turn_person=OWNER, owner="u-owner", binding_profile=None, **settings):
    agent = Agent(id=AGENT, description="d", model=AgentModel(profile_id="p"))
    agent.access.owners = [owner]
    await sp.get_storage(Agent).create(agent)
    mem = await sp.get_storage(AgentMemory).create(AgentMemory(id=AGENT, enabled=True, **settings))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id=AGENT, profile_id=binding_profile), created_at=NOW, turn_no=2, completed_turn_no=2,
        status=SessionStatus.RUNNING, turn_person=turn_person, initiated_by=OWNER,
    ))
    job = await sp.get_storage(MemoryJob).create(MemoryJob(
        id=f"reflect:{AGENT}:s1", kind="reflect", agent_id=AGENT, session_id="s1", reflected_turn=0, target_turn=2, created_at=NOW, expires_at=NOW,
    ))
    return agent, mem, job


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


def _deps(sp, llm, messages=None) -> MemoryDeps:
    async def load(_job):
        return messages if messages is not None else _transcript()

    return MemoryDeps(
        sp=sp, provider_registry=FakeRegistry(llm), semantic_search_registry=None, engine=None, recorder=recorder_for(sp),
        store=MemoryStore(sp, DocumentService(sp), recorder_for(sp)), now=lambda: NOW, load_messages=load,
    )


async def _entries(deps, status=None):
    return await deps.store.list_entries(AGENT, status)


async def _events(sp, prefix="memory.reflection"):
    return await sp.get_event_store().read_after(0, limit=50, event_type_prefix=prefix)


async def test_add_from_owner_run_lands_as_candidate_without_approval(sp, llm) -> None:
    agent, mem, job = await _setup(sp)
    llm.answers = [_run([_add_delta()])]
    deps = _deps(sp, llm)
    report = await reflect(deps, job, agent, mem)
    [entry] = await _entries(deps, "candidates")
    assert entry.meta["needs_approval"] is False and entry.meta["taught_by"] == "u-owner"
    assert entry.meta["sources"] == [{"session_id": "s1", "from_turn": 1, "to_turn": 2}]
    assert (report.written, report.dropped, report.run_succeeded, report.skipped_reason) == (1, 0, True, None)
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 2
    [event] = await _events(sp)
    assert event.event_type == "memory.reflection.completed" and event.entity_id == AGENT


async def test_add_from_non_owner_run_needs_approval_when_teachers_owner(sp, llm) -> None:
    agent, mem, job = await _setup(sp, turn_person=GUEST)
    llm.answers = [_run([_add_delta()])]
    deps = _deps(sp, llm)
    await reflect(deps, job, agent, mem)
    [entry] = await _entries(deps, "candidates")
    assert entry.meta["needs_approval"] is True and entry.meta["taught_by"] == "u-guest"


async def test_teachers_everyone_needs_no_approval_in_auto(sp, llm) -> None:
    agent, mem, job = await _setup(sp, turn_person=GUEST, teachers="everyone")
    llm.answers = [_run([_add_delta()])]
    deps = _deps(sp, llm)
    await reflect(deps, job, agent, mem)
    assert (await _entries(deps, "candidates"))[0].meta["needs_approval"] is False


async def test_approve_mode_marks_every_add_needs_approval(sp, llm) -> None:
    agent, mem, job = await _setup(sp, promotion="approve", teachers="everyone")
    llm.answers = [_run([_add_delta()])]
    deps = _deps(sp, llm)
    await reflect(deps, job, agent, mem)
    assert (await _entries(deps, "candidates"))[0].meta["needs_approval"] is True


async def _seed(deps, name="Known", **kw):
    add = MemoryAdd(**{**_add_delta(name), **{"op": "add"}})
    return await deps.store.add_candidate(AGENT, add, source={"session_id": "old", "from_turn": 1, "to_turn": 2}, taught_by="u-owner", needs_approval=kw.get("needs_approval", False))


def _read_call(path: str, call_id="r1") -> list[Message]:
    return [
        Message(role="assistant", parts=[ToolCallPart(id=call_id, name="collections__read_document", arguments={"collection": CID, "path": path})]),
        Message(role="tool", parts=[ToolResultPart(id=call_id, output="{}")]),
    ]


async def test_credit_ignored_for_unused_entries(sp, llm) -> None:
    agent, mem, job = await _setup(sp)
    deps = _deps(sp, llm)
    await _seed(deps, "Known")
    llm.answers = [_run([{"op": "credit", "name": "known", "verdict": "helpful", "reason": "r"}])]
    report = await reflect(deps, job, agent, mem)
    entry = await deps.store.get(AGENT, "known")
    assert entry.meta["helpful"] == 0 and entry.status == "candidates"
    assert (report.written, report.dropped) == (0, 1)


async def test_trial_candidate_promoted_on_helpful_success(sp, llm) -> None:
    agent, mem, job = await _setup(sp)
    notes = ExtendedPart.model_construct(extended=SimpleNamespace(type="memory_notes", paths=["active/other", "candidates/known"], trial="candidates/known"))
    deps = _deps(sp, llm, [Message(role="system", parts=[notes]), *_transcript()])
    await _seed(deps, "Known")
    llm.answers = [_run([{"op": "credit", "name": "known", "verdict": "helpful", "reason": "r"}], ok=True)]
    await reflect(deps, job, agent, mem)
    assert (await deps.store.get(AGENT, "known")).status == "active"


async def test_scrubbed_delta_is_dropped_others_kept(sp, llm) -> None:
    agent, mem, job = await _setup(sp)
    leaky = _add_delta("Mail ops", example="ask bob@example.com")
    llm.answers = [_run([leaky, _add_delta("Clean one")])]
    deps = _deps(sp, llm)
    report = await reflect(deps, job, agent, mem)
    assert [e.name for e in await _entries(deps, "candidates")] == ["clean-one"]
    assert (report.written, report.dropped) == (1, 1)


async def test_custom_reflection_prompt_is_sent_and_schema_still_enforced(sp, llm) -> None:
    agent, mem, job = await _setup(sp, reflection_prompt="CUSTOM PROMPT")
    llm.answers = [_run([_add_delta()])]
    deps = _deps(sp, llm)
    await reflect(deps, job, agent, mem)
    assert llm.calls[0]["system_prompt"] == "CUSTOM PROMPT" != DEFAULT_REFLECTION_PROMPT
    assert "deltas" in llm.calls[0]["schema"]["properties"]
    llm.answers = [_run([_add_delta("x1")] * 4)]       # a custom prompt does not loosen the cap
    job2 = job.model_copy(update={"reflected_turn": 0})
    report = await reflect(deps, job2, agent, mem)
    assert report.skipped_reason == "invalid_output"


@pytest.mark.parametrize("answer", [ValueError("not json"), {"run_succeeded": True, "deltas": [_add_delta(op="nope")]}, _run([_add_delta(f"n{i}") for i in range(4)]), {"deltas": []}])
async def test_invalid_output_writes_nothing_and_skips(sp, llm, answer) -> None:
    agent, mem, job = await _setup(sp)
    llm.answers = [answer]
    deps = _deps(sp, llm)
    report = await reflect(deps, job, agent, mem)
    assert report.skipped_reason == "invalid_output" and report.written == 0
    assert await _entries(deps) == []
    [event] = await _events(sp)
    assert event.event_type == "memory.reflection.skipped" and event.payload["reason"] == "invalid_output"
    assert len(llm.calls) == 1                                      # no retry loop


async def test_unresolvable_profile_skips(sp, llm) -> None:
    agent, mem, job = await _setup(sp)
    await sp.get_storage(ModelProfile).delete("p")
    deps = _deps(sp, llm)
    report = await reflect(deps, job, agent, mem)
    assert report.skipped_reason == "profile_unavailable" and llm.calls == []
    [event] = await _events(sp)
    assert event.event_type == "memory.reflection.skipped" and event.payload["reason"] == "profile_unavailable"


async def test_uses_agent_profile_not_session_override(sp, llm) -> None:
    agent, mem, job = await _setup(sp, binding_profile="other")
    await sp.get_storage(ModelProfile).create(ModelProfile(id="other", description="o", provider_id="other-prov", model_name="m2", context_length=1000))
    llm.answers = [_run([])]
    deps = _deps(sp, llm)
    await reflect(deps, job, agent, mem)
    assert deps.provider_registry.asked == ["prov"] and llm.calls[0]["model"] == "m1"


def test_used_entries_reads_search_and_read_document_calls_on_this_collection_only() -> None:
    search = '{"hits": [{"path": "active/one", "excerpt": "x"}, {"path": "candidates/two", "excerpt": "y"}, {"path": "archived/gone", "excerpt": "z"}], "mode_used": "fulltext"}'
    messages = [
        Message(role="assistant", parts=[
            ToolCallPart(id="s1", name="collections__search", arguments={"collection": CID, "query": "q"}),
            ToolCallPart(id="s2", name="collections__search", arguments={"collection": "kb", "query": "q"}),
            ToolCallPart(id="r1", name="collections__read_document", arguments={"collection": CID, "path": "active/three"}),
            ToolCallPart(id="r2", name="collections__read_document", arguments={"collection": "kb", "path": "active/not-mine"}),
            ToolCallPart(id="r3", name="collections__read_document", arguments={"collection": CID, "path": "active/failed"}),
        ]),
        Message(role="tool", parts=[
            ToolResultPart(id="s1", output=search),
            ToolResultPart(id="s2", output=search.replace("one", "elsewhere")),
            ToolResultPart(id="r1", output="{}"),
            ToolResultPart(id="r2", output="{}"),
            ToolResultPart(id="r3", output="boom", error=True),
        ]),
    ]
    assert used_entries(messages, CID) == ({"one", "two", "three"}, None)
    notes = ExtendedPart.model_construct(extended=SimpleNamespace(type="memory_notes", paths=["active/four"], trial="candidates/five"))
    names, trial = used_entries([Message(role="system", parts=[notes]), *messages], CID)
    assert names == {"one", "two", "three", "four", "five"} and trial == "five"
