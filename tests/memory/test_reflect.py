"""Reflection (agent memory T7): a scripted LLM, a real store over SQLite."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from primer.events.recorder import recorder_for
from primer.knowledge.document_service import DocumentService
from primer.memory.collection import memory_collection_id
from primer.memory.jobs import MemoryDeps
from primer.memory.reflect import DEFAULT_REFLECTION_PROMPT, reflect, used_entries
from primer.memory.store import MemoryStore
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Message, TextPart, ToolCallPart, ToolResultPart
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

    async def stream(self, *, model, messages, response_format, tools, tool_choice, max_output_tokens, **kwargs):
        from primer.model.chat import Done, TextDelta
        assert tools is None and tool_choice is None and max_output_tokens > 0
        try:
            answer = await self.judge_structured(model=model, system_prompt=messages[0].parts[0].text,
                                                user_message=messages[1].parts[0].text, response_schema=response_format)
        except ValueError:
            yield TextDelta(text="not json", index=0)
        else:
            yield TextDelta(text=json.dumps(answer), index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

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
        ModelProfile(id="p", description="p", provider_id="prov", model_name="m1", context_length=32768)
    )
    yield provider
    await provider.aclose()


async def _setup(sp, *, turn_person=OWNER, owner="u-owner", binding_profile=None, **settings):
    agent = Agent(id=AGENT, description="d", model=AgentModel(profile_id="p"))
    agent.access.owners = [owner]
    agent = await sp.get_storage(Agent).create(agent)
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


def _deps(sp, llm, messages=None, store=None) -> MemoryDeps:
    async def load(_job):
        return messages if messages is not None else _transcript()

    return MemoryDeps(
        sp=sp, provider_registry=FakeRegistry(llm), semantic_search_registry=None, engine=None, recorder=recorder_for(sp),
        store=store if store is not None else MemoryStore(sp, DocumentService(sp), recorder_for(sp)), now=lambda: NOW, load_messages=load,
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


async def _lookup_event(sp, *, turn: int, paths: list[str], trial: str | None = None, session_id: str = "s1") -> None:
    await recorder_for(sp).emit(
        "memory.lookup", payload={"session_id": session_id, "turn": turn, "paths": paths, "trial": trial, "scores": [0.9], "mode": "semantic", "injected": len(paths)},
        actor=f"memory:{AGENT}", entity_kind="agent", entity_id=AGENT, session_id=session_id,
    )


async def test_reflection_credits_an_injected_entry_from_the_lookup_event(sp, llm) -> None:
    """The notes the per-turn lookup showed are in no log: the ``memory.lookup`` event of the session is the citation."""
    agent, mem, job = await _setup(sp)
    deps = _deps(sp, llm)
    await deps.store.approve(AGENT, (await _seed(deps, "Known")).name, actor="user:o")
    await _lookup_event(sp, turn=2, paths=["active/known"])
    llm.answers = [_run([{"op": "credit", "name": "known", "verdict": "helpful", "reason": "r"}])]
    report = await reflect(deps, job, agent, mem)
    assert (await deps.store.get(AGENT, "known")).meta["helpful"] == 1 and (report.written, report.dropped) == (1, 0)


async def test_lookups_of_other_sessions_and_other_turns_are_not_citations(sp, llm) -> None:
    agent, mem, job = await _setup(sp)
    deps = _deps(sp, llm)
    await deps.store.approve(AGENT, (await _seed(deps, "Known")).name, actor="user:o")
    await _lookup_event(sp, turn=2, paths=["active/known"], session_id="another")
    await _lookup_event(sp, turn=3, paths=["active/known"])  # after the reflected range (1..2)
    llm.answers = [_run([{"op": "credit", "name": "known", "verdict": "helpful", "reason": "r"}])]
    report = await reflect(deps, job, agent, mem)
    assert (await deps.store.get(AGENT, "known")).meta["helpful"] == 0 and (report.written, report.dropped) == (0, 1)


async def test_trial_candidate_promoted_on_helpful_success(sp, llm) -> None:
    agent, mem, job = await _setup(sp)
    deps = _deps(sp, llm)
    await _lookup_event(sp, turn=2, paths=["active/other", "candidates/known"], trial="candidates/known")
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
    job2 = job.model_copy(update={"id": "reflect-second", "reflected_turn": 0})
    await sp.get_storage(MemoryJob).create(job2)
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


class ProcessKilled(Exception):
    """A simulated SIGKILL: nothing later in the run executes."""


class CrashAfterCreditStore(MemoryStore):
    """The real store, except the process dies right after a credit write completes and before the job is marked."""

    def __init__(self, sp, documents, recorder) -> None:
        super().__init__(sp, documents, recorder)
        self._crash_after_credit = True

    async def credit(self, *args, **kwargs):
        entry = await super().credit(*args, **kwargs)
        if self._crash_after_credit:
            self._crash_after_credit = False
            raise ProcessKilled("killed after the credit write, before the mark")
        return entry


async def _clean_credit_run(tmp_path: Path, llm: FakeLLM) -> dict:
    """A non-crashed credit run over a fresh provider: the final state a crashed run's replay must equal."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "clean.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    await sp.get_event_store().ensure_schema()
    await sp.get_storage(Collection).create(Collection(id=CID, description="memory", memory_of=AGENT))
    await sp.get_storage(ModelProfile).create(
        ModelProfile(id="p", description="p", provider_id="prov", model_name="m1", context_length=32768)
    )
    agent, mem, job = await _setup(sp)
    deps = _deps(sp, llm)
    await _seed(deps, "Known")
    await deps.store.approve(AGENT, "Known", actor="user:o")
    await _lookup_event(sp, turn=2, paths=["active/known"])
    llm.answers = [{"run_succeeded": True, "deltas": [{"op": "credit", "name": "known", "verdict": "helpful", "reason": "r"}]}]
    await reflect(deps, job, agent, mem)
    state = {
        "meta": (await deps.store.get(AGENT, "known")).meta,
        "reflected_turn": (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn,
    }
    await sp.aclose()
    return state


async def test_a_crash_before_the_mark_replays_the_run_as_a_storage_no_op(sp, llm, tmp_path) -> None:
    """A SIGKILL between a delta application and the mark: the re-claim re-runs the run, and the replay must be a pure no-op at the
    storage level - the credit applied exactly once, the job marked, and the final memory state equal to the clean non-crashed run's."""
    agent, mem, job = await _setup(sp)
    deps = _deps(sp, llm, store=CrashAfterCreditStore(sp, DocumentService(sp), recorder_for(sp)))
    await _seed(deps, "Known")
    await deps.store.approve(AGENT, "Known", actor="user:o")
    await _lookup_event(sp, turn=2, paths=["active/known"])
    credit_delta = {"op": "credit", "name": "known", "verdict": "helpful", "reason": "r"}
    llm.answers = [_run([credit_delta]), _run([credit_delta])]

    with pytest.raises(ProcessKilled):
        await reflect(deps, job, agent, mem)
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0   # the mark never ran

    report = await reflect(_deps(sp, llm), job, agent, mem)                  # the re-claim re-runs the same job
    entry = await deps.store.get(AGENT, "known")
    assert entry.meta["helpful"] == 1                                         # the credit applied exactly once
    assert (report.written, report.dropped) == (1, 0)
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 2  # the job is marked

    clean = await _clean_credit_run(tmp_path, llm)
    # the meta must be identical, except the wall-clock created_at the test's own seeding stamped (seeds differ by milliseconds)
    assert {k: v for k, v in entry.meta.items() if k != "created_at"} == {k: v for k, v in clean["meta"].items() if k != "created_at"}
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == clean["reflected_turn"] == 2


async def test_a_clean_reflect_run_is_unchanged_by_the_replay_guard(sp, llm) -> None:
    """The no-fault run is unchanged: the deltas apply, the job is marked, the credit once (guards the happy path against the guard)."""
    agent, mem, job = await _setup(sp)
    deps = _deps(sp, llm)
    await _seed(deps, "Known")
    await deps.store.approve(AGENT, "Known", actor="user:o")
    await _lookup_event(sp, turn=2, paths=["active/known"])
    llm.answers = [_run([{"op": "credit", "name": "known", "verdict": "helpful", "reason": "r"}, _add_delta()])]

    report = await reflect(deps, job, agent, mem)

    assert (await deps.store.get(AGENT, "known")).meta["helpful"] == 1
    assert [e.name for e in await _entries(deps, "candidates")] == ["retry-github-429"]
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 2
    assert (report.written, report.dropped) == (2, 0)


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
    lookups = [{"paths": ["active/four", "candidates/five"], "trial": "candidates/five"}, {"paths": ["active/six"], "trial": None}]
    names, trial = used_entries(messages, CID, lookups)
    assert names == {"one", "two", "three", "four", "five", "six"} and trial == "five"


@pytest.mark.parametrize("failure", ["too_large", "policy", "invalid_summary", "invalid_second_summary", "superseded", "disabled"])
async def test_input_failure_preserves_watermark_and_entries(sp, llm, failure):
    from primer.model.chat import Done, TextDelta
    agent, mem, job = await _setup(sp)
    source = _transcript()
    source[0] = Message(role="user", parts=[TextPart(text="early correction " + "x" * (600000 if failure == "too_large" else 140000))])
    if failure == "policy":
        mem = await sp.get_storage(AgentMemory).update(mem.model_copy(update={"do_not_capture": ["x" * 150000]}))
    calls = []
    class SummaryLLM(FakeLLM):
        async def stream(self, **kwargs):
            calls.append(kwargs)
            if failure == "superseded":
                await sp.get_storage(MemoryJob).patch_if(job.id, {"target_turn": 3}, where=job.occurrence_guard())
            if failure == "disabled":
                assert await sp.get_storage(AgentMemory).patch_if(AGENT, {"enabled": False}, where={"enabled": [True]})
            yield TextDelta(text='{"facts":[]}' if failure in {"superseded", "disabled"} or (failure == "invalid_second_summary" and len(calls) == 1) else '{"facts":[{"observation":"invalid","sources":["invented"]}]}', index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
    deps = _deps(sp, SummaryLLM(), source)
    before = json.dumps([m.model_dump(mode="json") for m in source])
    report = await reflect(deps, job, agent, mem)
    expected = {"too_large": "input_compaction_limit", "policy": "input_policy_too_large",
                "invalid_summary": "compaction_invalid", "invalid_second_summary": "compaction_invalid",
                "superseded": "input_superseded", "disabled": "input_superseded"}[failure]
    assert report.skipped_reason == expected
    assert await _entries(deps) == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0
    assert json.dumps([m.model_dump(mode="json") for m in source]) == before
    if failure in {"too_large", "policy"}:
        assert calls == []
    else:
        assert len(calls) == (2 if failure == "invalid_second_summary" else 1)


async def test_actual_reflection_learns_from_early_evidence_after_compaction(sp):
    from primer.model.chat import Done, TextDelta
    agent, mem, job = await _setup(sp)
    source = _transcript()
    source[0] = Message(role="user", parts=[TextPart(text="EARLY_RETRY_CORRECTION " + "x" * 140000)])
    observed = []
    class EvidenceLLM(FakeLLM):
        async def stream(self, **kwargs):
            user = kwargs["messages"][1].parts[0].text
            observed.append(user)
            if user.startswith("# Memory evidence to summarize"):
                rows = [json.loads(line) for line in user.splitlines()[1:]]
                facts = [{"observation": "EARLY_RETRY_CORRECTION: wait before retrying", "sources": [r["source"]]}
                         for r in rows if "EARLY_RETRY_CORRECTION" in r["text"]]
                answer = {"facts": facts}
            else:
                assert "lossy evidence summary" in user
                assert "EARLY_RETRY_CORRECTION: wait before retrying" in user
                answer = _run([_add_delta()])
            yield TextDelta(text=json.dumps(answer), index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
    deps = _deps(sp, EvidenceLLM(), source)
    report = await reflect(deps, job, agent, mem)
    assert report.written == 1
    assert 2 < len(observed) <= 5
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 2
    events = await _events(sp)
    completed = next(e for e in events if e.event_type == "memory.reflection.completed")
    assert completed.payload["input_mode"] == "compacted"
    assert completed.payload["summary_calls"] == len(observed) - 1
    assert completed.payload["input_estimate"] <= completed.payload["input_budget"]
    assert "EARLY_RETRY_CORRECTION" not in json.dumps(completed.payload)


async def test_tiny_profile_refuses_complete_frame_without_model_call(sp, llm):
    agent, mem, job = await _setup(sp)
    await sp.get_storage(ModelProfile).patch_if("p", {"context_length": 1000}, where={"context_length": [32768]})
    deps = _deps(sp, llm)
    report = await reflect(deps, job, agent, mem)
    assert report.skipped_reason == "input_policy_too_large" and llm.calls == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0


@pytest.mark.parametrize("available", [0, 1])
async def test_incomplete_transcript_never_advances_requested_range(sp, llm, available):
    agent, mem, job = await _setup(sp)
    source = _transcript()[:3] if available else []
    deps = _deps(sp, llm, source)
    report = await reflect(deps, job, agent, mem)
    assert report.skipped_reason == "input_incomplete" and llm.calls == []
    assert await _entries(deps) == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0


@pytest.mark.parametrize("finish,parseable", [("length", False), ("length", True), ("stop", False)])
async def test_final_token_exhaustion_never_applies_or_advances(sp, finish, parseable):
    from types import SimpleNamespace as NS
    from primer.llm._openai_compat import _StreamState, _translate_chunk

    agent, mem, job = await _setup(sp)
    text = json.dumps(_run([_add_delta()])) if parseable else '{"run_succeeded":true,"deltas":['
    class Limited(FakeLLM):
        async def stream(self, **kwargs):
            state = _StreamState()
            for content, reason in ((text, None), (None, finish)):
                chunk = NS(id="offline", model="m", usage=None, choices=[
                    NS(index=0, delta=NS(role="assistant", content=content, tool_calls=None), finish_reason=reason),
                ])
                for event in _translate_chunk(chunk, state):
                    yield event
    deps = _deps(sp, Limited())
    report = await reflect(deps, job, agent, mem)
    assert report.skipped_reason == ("compaction_output_limit" if finish == "length" else "invalid_output")
    assert await _entries(deps) == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == (0 if finish == "length" else 2)


@pytest.mark.parametrize("edit", ["disable", "exclusion"])
@pytest.mark.parametrize("answer", ["malformed", '{"run_succeeded":"not a boolean"}'])
async def test_invalid_final_output_rechecks_policy_before_progress(sp, edit, answer):
    import asyncio
    from primer.model.chat import Done, TextDelta
    agent, mem, job = await _setup(sp)
    entered, release = asyncio.Event(), asyncio.Event()
    class Held(FakeLLM):
        async def stream(self, **kwargs):
            entered.set()
            await release.wait()
            yield TextDelta(text=answer, index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
    deps = _deps(sp, Held())
    running = asyncio.create_task(reflect(deps, job, agent, mem))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            patch = {"enabled": False} if edit == "disable" else {"do_not_capture": ["new owner exclusion"]}
            assert await sp.get_storage(AgentMemory).patch_if(AGENT, patch, where={"enabled": [True]})
            release.set()
            report = await running
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert report.skipped_reason == "input_superseded"
    assert await _entries(deps) == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0


async def test_session_identity_is_captured_before_loading_evidence(sp, llm):
    agent, mem, job = await _setup(sp)
    deps = _deps(sp, llm)
    async def changed_during_load(_job):
        assert await sp.get_storage(WorkspaceSession).patch_if("s1", {"binding_epoch": 1}, where={"binding_epoch": [0]})
        return _transcript()
    deps.load_messages = changed_during_load
    report = await reflect(deps, job, agent, mem)
    assert report.skipped_reason == "input_superseded" and llm.calls == []
    assert await _entries(deps) == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0


@pytest.mark.parametrize("stage", ["summary", "final"])
@pytest.mark.parametrize("ending", ["failed", "error_event", "missing", "filtered", "refused"])
@pytest.mark.parametrize("parseable", [False, True])
async def test_unconfirmed_response_preserves_all_progress(sp, stage, ending, parseable):
    from types import SimpleNamespace as NS
    from primer.llm.openresponses import _StreamState, _translate_event
    from primer.memory.reflect import ReflectionTransient

    agent, mem, job = await _setup(sp)
    source = _transcript()
    if stage == "summary":
        source[0] = Message(role="user", parts=[TextPart(text="early correction " + "x" * 140000)])
    answer = {"facts": []} if stage == "summary" else _run([_add_delta()])
    text = json.dumps(answer) if parseable else ("" if ending == "refused" else '{"partial":')
    calls = []
    class Incomplete(FakeLLM):
        async def stream(self, **kwargs):
            calls.append(kwargs)
            state = _StreamState()
            events = [NS(type="response.output_text.delta", delta=text)]
            if ending == "error_event":
                events.append(NS(type="error", code="server_error", message="offline failure"))
            if ending in {"failed", "error_event"}:
                events.append(NS(type="response.failed"))
            elif ending == "filtered":
                events.append(NS(type="response.incomplete", response=NS(incomplete_details=NS(reason="content_filter"))))
            elif ending == "refused":
                events.extend([NS(type="response.refusal.delta", delta="offline refusal"),
                               NS(type="response.completed")])
            for raw in events:
                for event in _translate_event(raw, state):
                    yield event
    deps = _deps(sp, Incomplete(), source)
    if ending in {"filtered", "refused"}:
        report = await reflect(deps, job, agent, mem)
        assert report.skipped_reason == "compaction_incomplete"
    else:
        with pytest.raises(ReflectionTransient):
            await reflect(deps, job, agent, mem)
    assert len(calls) == 1 and await _entries(deps) == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0
