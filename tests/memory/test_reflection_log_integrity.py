"""Reflection-only strict log reads and real job disposition over SQLite."""
import json

import pytest

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.memory.jobs import run_memory_job
from primer.memory.reflect import reflect, session_message_loader
from primer.memory.reflection_input import InputRefused
from primer.model.memory import MemoryJob
from primer.model.workspace_session import WorkspaceSession
from tests.memory.test_reflect import (
    NOW, FakeLLM, _deps, _entries, _run, _setup, _transcript, sp,  # noqa: F401 (fixture)
)


@pytest.mark.parametrize("tail", [b'{"role":"assistant",', b'\xff', b'[]', b'{"role":"invalid","parts":[]}'])
@pytest.mark.parametrize("through_job", [False, True])
async def test_torn_log_after_enough_user_starts_preserves_progress(sp, tail, through_job):
    agent, settings, job = await _setup(sp)
    raw = ("\n".join(m.model_dump_json() for m in _transcript()) + "\n").encode() + tail
    async def read_log(workspace, slot):
        assert (workspace, slot) == ("w1", "sessions/s1")
        return raw
    llm = FakeLLM()
    deps = _deps(sp, llm)
    deps.load_messages = session_message_loader(sp, read_log)
    if through_job:
        engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
        await engine.upsert(ClaimKind.MEMORY, job.id)
        lease, = await engine.claim_due("offline", max_count=1)
        deps.engine = engine
        outcome = await run_memory_job(deps, lease)
        assert outcome.drop_lease
    else:
        report = await reflect(deps, job, agent, settings)
        assert report.skipped_reason == "input_incomplete"
    assert llm.calls == [] and await _entries(deps) == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0
    events = await sp.get_event_store().read_after(0, event_type_prefix="memory.reflection.skipped")
    assert events[-1].payload["reason"] == "input_incomplete"


async def test_loader_retains_original_compacted_evidence_and_honors_rewinds(sp):
    from tests._support.segmented_log import full_run
    _, _, job = await _setup(sp)
    raw = full_run()
    async def read_log(*args):
        return raw
    messages = await session_message_loader(sp, read_log)(job)
    text = "\n".join(getattr(p, "text", "") for m in messages for p in m.parts)
    assert "first question" in text and "first answer" in text and "sixth answer" in text
    assert "fourth question" not in text and "SUMMARY of the first turn" not in text
    assert raw == full_run()


async def test_loader_rejects_identity_changed_during_actual_read(sp):
    _, _, job = await _setup(sp)
    async def read_log(*args):
        assert await sp.get_storage(WorkspaceSession).patch_if("s1", {"workspace_id": "other"}, where={"workspace_id": ["w1"]})
        return ("\n".join(m.model_dump_json() for m in _transcript()) + "\n").encode()
    with pytest.raises(InputRefused, match="input_superseded"):
        await session_message_loader(sp, read_log)(job)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("through_job", [False, True])
async def test_writer_unicode_separators_are_not_record_boundaries(sp, newline, through_job):
    from primer.model.chat import Message, TextPart
    agent, settings, job = await _setup(sp)
    text = "keep\u2028line\u2029paragraph\u0085next"
    messages = _transcript()
    messages[0] = Message(role="user", parts=[TextPart(text=text)])
    raw = (newline.join(m.model_dump_json() for m in messages) + newline).encode()
    assert all(separator.encode() in raw for separator in ("\u2028", "\u2029", "\u0085"))
    async def read_log(*args):
        return raw
    llm = FakeLLM()
    llm.answers = [_run([])]
    deps = _deps(sp, llm)
    deps.load_messages = session_message_loader(sp, read_log)
    assert (await deps.load_messages(job))[0].parts[0].text == text
    if through_job:
        engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
        await engine.upsert(ClaimKind.MEMORY, job.id)
        lease, = await engine.claim_due("offline", max_count=1)
        deps.engine = engine
        await run_memory_job(deps, lease)
    else:
        assert (await reflect(deps, job, agent, settings)).skipped_reason is None
    assert len(llm.calls) == 1 and text in llm.calls[0]["user_message"]
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 2


@pytest.mark.parametrize("target", [99, 5, 2, 0, True])
async def test_impossible_rewind_after_two_turns_never_reaches_model(sp, target):
    _, _, job = await _setup(sp)
    messages = [m.model_dump_json() for m in _transcript()]
    def record(seq, kind, **payload):
        return json.dumps({"seq": seq, "kind": kind, "payload": payload})
    lines = [record(1, "user_input"), *messages[:3], record(2, "tool_result"),
             record(3, "user_input"), *messages[3:], record(4, "done"),
             record(5, "rewind_marker", to_seq=target)]
    async def read_log(*args):
        return ("\n".join(lines) + "\n").encode()
    llm = FakeLLM()
    deps = _deps(sp, llm)
    deps.load_messages = session_message_loader(sp, read_log)
    engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
    await engine.upsert(ClaimKind.MEMORY, job.id)
    lease, = await engine.claim_due("offline", max_count=1)
    deps.engine = engine
    assert (await run_memory_job(deps, lease)).drop_lease
    assert llm.calls == [] and await _entries(deps) == []
    assert (await sp.get_storage(MemoryJob).get(job.id)).reflected_turn == 0


@pytest.mark.parametrize("sequenced_marker", [False, True])
async def test_historical_rewind_keeps_prior_visible_target_across_compaction(sp, sequenced_marker):
    from primer.model.chat import Message, TextPart
    _, _, job = await _setup(sp)
    def message(role, text):
        return Message(role=role, parts=[TextPart(text=text)]).model_dump(mode="json")
    marker = {"kind": "rewind_marker", "payload": {"to_seq": 1}}
    if sequenced_marker:
        marker["seq"] = 4
    rows = [message("user", "original"), {"seq": 1, "kind": "user_input"},
            message("assistant", "original answer"), {"seq": 2, "kind": "user_input"},
            message("user", "discarded"), message("assistant", "discarded answer"),
            {"seq": 3, "kind": "compaction_marker", "payload": {"summary": "folded"}}, marker,
            {"seq": 5, "kind": "user_input"}, message("user", "later")]
    async def read_log(*args):
        return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    result = await session_message_loader(sp, read_log)(job)
    texts = [p.text for m in result for p in m.parts]
    assert texts == ["original", "original answer", "later"]


@pytest.mark.parametrize("fault", ["out_of_order", "hidden_target"])
async def test_rewind_requires_ordered_still_visible_evidence(sp, fault):
    _, _, job = await _setup(sp)
    rows = [{"seq": 1, "kind": "user_input"}, {"seq": 2, "kind": "done"},
            {"seq": 3, "kind": "user_input"}, {"seq": 4, "kind": "done"}]
    if fault == "out_of_order":
        rows.append({"seq": 3, "kind": "rewind_marker", "payload": {"to_seq": 1}})
    else:
        rows += [{"seq": 5, "kind": "rewind_marker", "payload": {"to_seq": 1}},
                 {"seq": 6, "kind": "user_input"}, {"seq": 7, "kind": "done"},
                 {"seq": 8, "kind": "rewind_marker", "payload": {"to_seq": 3}}]
    async def read_log(*args):
        return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    with pytest.raises(InputRefused, match="input_incomplete"):
        await session_message_loader(sp, read_log)(job)
