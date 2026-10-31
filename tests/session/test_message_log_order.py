"""The order a turn's Message lines and event records land in ``messages.jsonl`` (AG-M2).

Message lines carry no seq: the history reader tags each one with the last seq seen before it, and a rewind
to seq S keeps exactly the Message lines tagged at or below S. The executor used to write the turn's Message
lines while the dispatch writer still buffered the turn's records, so the answer of a turn sat in the file
before its own ``llm_call`` / ``assistant_token`` records, was tagged with the turn's ``user_input`` seq, and
survived a rewind to that seq: the transcript hid the answer and the next turn's model still saw it.

Real ``wake_session`` + ``run_one_session_turn`` + ``WorkspaceAgentExecutor`` over a real local workspace, the
dispatch writer appending into the same ``messages.jsonl`` (the ``_WorkspaceIOShim`` production path).

The characterization tests pin what a turn records without a rewind: the event records (seq and kind, in file
order), the LLM history the next turn is built from, and the state repo's commit subjects. They pass before and
after the fix; only the position of the Message lines among the records moves.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.agent.compaction import CompactionStrategy
from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Done, TextDelta, TextPart, ToolCallEnd, ToolCallStart
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.replay import visible_records
from primer.session.rewind import append_rewind_marker, check_rewind_target
from primer.worker.io_shim import _WorkspaceIOShim
from primer.workspace.session import reconstruct_compacted_history
from tests.agent.test_workspace_executor import _agent, _build_session, _FakeLLM, _model
from tests.session.test_dispatch import _make_lease, fake_event_bus, fake_storage_provider  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
POSIX = pytest.mark.skipif(not Path("/usr/bin/env").exists(), reason="needs a POSIX shell for the exec tool")


class _Registry:
    def __init__(self, ws):
        self.ws = ws

    async def get_workspace(self, wid):
        return self.ws

    async def get_workspace_row(self, wid):
        return None


class _Sched:
    async def enqueue(self, sid):
        ...


class _Eng:
    async def upsert(self, *a, **k):
        ...


def _ans(text):
    return [TextDelta(text=text, index=0), Done(stop_reason="stop", raw_reason="stop")]


def _exec_call(call_id):
    return [
        ToolCallStart(id=call_id, name="workspace__exec", index=0),
        ToolCallEnd(id=call_id, arguments={"command": "true", "description": "noop"}, index=0),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


class _Harness:
    """One session on a local workspace, driven turn by turn through the real wake and dispatch."""

    def __init__(self, tmp_path, storage, bus, scripts, *, compaction=None):
        self.tmp_path, self.storage, self.bus, self.scripts, self.compaction = tmp_path, storage, bus, scripts, compaction

    async def __aenter__(self):
        self.backend, self.workspace, self.slot = await _build_session(self.tmp_path)
        self.sid = self.slot.session_id
        self.sessions = self.storage.get_storage(WorkspaceSession)
        await self.sessions.create(WorkspaceSession(
            id=self.sid, workspace_id=self.workspace.id, binding=AgentSessionBinding(agent_id="researcher"),
            status=SessionStatus.CREATED, created_at=datetime.now(timezone.utc),
        ))
        self.llm = _FakeLLM(scripts=self.scripts)
        reg = _Registry(self.workspace)
        self.shim = _WorkspaceIOShim(workspace_registry=reg)
        self.shim.register_session(self.sid, self.workspace.id)

        async def build(row):
            s = await self.workspace.get_session(row.id)
            mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=s)
            return WorkspaceAgentExecutor(
                agent=_agent(), llm=self.llm, llm_model=_model(), tool_manager=mgr, session=s,
                compaction=self.compaction,
            )

        self.deps = SessionDispatchDeps(
            storage_provider=self.storage, workspace_io=self.shim, event_bus=self.bus, build_executor=build,
            scheduler=_Sched(), claim_engine=_Eng(), workspace_registry=reg,
        )
        self.wdeps = SessionWakeDeps(
            storage_provider=self.storage, scheduler=_Sched(), claim_engine=_Eng(), workspace_registry=reg,
            event_bus=self.bus,
        )
        return self

    async def __aexit__(self, *exc):
        await self.backend.aclose()

    async def turn(self, instruction):
        await wake_session(
            workspace_id=self.workspace.id, session_id=self.sid, instruction=instruction, human_intent=True,
            deps=self.wdeps,
        )
        await run_one_session_turn(_make_lease(self.sid), self.deps)
        # the claim adapter's release bumps turn_no; without it the next claim is the no-op of an already-completed turn
        row = await self.sessions.get(self.sid)
        await self.sessions.update(row.model_copy(update={"turn_no": row.turn_no + 1}))

    async def lines(self):
        return (await self.slot._state.read_state_file(f"sessions/{self.sid}/messages.jsonl")).decode().splitlines()

    async def records(self):
        return [(o["seq"], o["kind"]) for o in map(json.loads, await self.lines()) if "seq" in o]

    async def history(self):
        return [(m.role, _text(m)) for m in reconstruct_compacted_history(await self.lines())]

    def subjects(self):
        out = subprocess.run(
            ["git", "-C", str(self.workspace.root / self.workspace.template.state_path), "log", "--format=%s"],
            check=True, capture_output=True, text=True,
        ).stdout
        return [s.replace(self.sid, "<sid>").replace(self.sid[-12:], "<sid>") for s in out.splitlines()]

    async def rewind(self, to_seq):
        check_rewind_target(await self.lines(), to_seq=to_seq)
        row = await self.sessions.get(self.sid)
        await append_rewind_marker(sessions=self.sessions, row=row, workspace_io=self.shim, to_seq=to_seq, actor="user")


def _text(message):
    return "".join(p.text for p in message.parts if isinstance(p, TextPart))


def _user_input_seqs(records):
    return [s for s, k in records if k == "user_input"]


# ---------------------------------------------------------------------------
# Characterization: what a turn records without a rewind
# ---------------------------------------------------------------------------


async def test_a_plain_turn_records_and_history(tmp_path, fake_storage_provider, fake_event_bus):
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, [_ans("ANSWER-ONE."), _ans("ANSWER-TWO.")]) as h:
        before = h.subjects()
        await h.turn("QUESTION-ONE")
        await h.turn("QUESTION-TWO")
        assert await h.records() == PLAIN_RECORDS
        assert await h.history() == [
            ("user", "hello"), ("user", "QUESTION-ONE"), ("assistant", "ANSWER-ONE."),
            ("user", "QUESTION-TWO"), ("assistant", "ANSWER-TWO."),
        ]
        assert h.subjects()[: len(h.subjects()) - len(before)] == PLAIN_SUBJECTS


@POSIX
async def test_a_tool_turn_records_and_history(tmp_path, fake_storage_provider, fake_event_bus):
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, [_exec_call("call_a"), _ans("DONE.")]) as h:
        before = h.subjects()
        await h.turn("RUN IT")
        assert await h.records() == TOOL_RECORDS
        history = await h.history()
        assert [r for r, _t in history] == ["user", "user", "assistant", "tool", "assistant"]
        assert history[1] == ("user", "RUN IT") and history[-1] == ("assistant", "DONE.")
        assert h.subjects()[: len(h.subjects()) - len(before)] == TOOL_SUBJECTS


async def test_a_compaction_turn_records_and_history(tmp_path, fake_storage_provider, fake_event_bus):
    # about 50k + 60k tokens of history: the second turn is over the trigger of the 128k model and compacts
    async with _Harness(
        tmp_path, fake_storage_provider, fake_event_bus,
        [_ans("a" * 240_000), _ans("SUMMARY"), _ans("A2.")], compaction=CompactionStrategy(tail_turns=1),
    ) as h:
        before = h.subjects()
        await h.turn("q" * 200_000)
        await h.turn("Q2")
        assert await h.records() == COMPACTION_RECORDS
        history = await h.history()
        assert [r for r, _t in history] == ["assistant", "user", "assistant"]
        assert history[0][1].endswith("\n\nSUMMARY") and history[1:] == [("user", "Q2"), ("assistant", "A2.")]
        assert h.subjects()[: len(h.subjects()) - len(before)] == COMPACTION_SUBJECTS


# ---------------------------------------------------------------------------
# AG-M2: a rewind takes the rewound answer out of the model's history too
# ---------------------------------------------------------------------------


async def test_a_rewind_to_the_turns_input_drops_its_answer_from_the_llm_history(
    tmp_path, fake_storage_provider, fake_event_bus,
):
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, [_ans("ANSWER-ONE."), _ans("ANSWER-TWO.")]) as h:
        await h.turn("QUESTION-ONE")
        first_ui = _user_input_seqs(await h.records())[0]
        await h.rewind(first_ui)
        lines = await h.lines()
        visible = [(r["seq"], r["kind"]) for r in visible_records(lines)]
        assert not any(k == "assistant_token" for _s, k in visible)
        assert await h.history() == [("user", "hello"), ("user", "QUESTION-ONE")]


@POSIX
async def test_a_rewind_to_a_tool_turns_input_drops_the_whole_round(tmp_path, fake_storage_provider, fake_event_bus):
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, [_exec_call("call_a"), _ans("DONE.")]) as h:
        await h.turn("RUN IT")
        await h.rewind(_user_input_seqs(await h.records())[0])
        assert await h.history() == [("user", "hello"), ("user", "RUN IT")]


async def test_the_next_turn_after_a_rewind_does_not_see_the_rewound_answer(
    tmp_path, fake_storage_provider, fake_event_bus,
):
    async with _Harness(tmp_path, fake_storage_provider, fake_event_bus, [_ans("ANSWER-ONE."), _ans("ANSWER-TWO.")]) as h:
        await h.turn("QUESTION-ONE")
        await h.rewind(_user_input_seqs(await h.records())[0])
        await h.turn("QUESTION-TWO")
        prompt = [(m.role, _text(m)) for m in h.llm.calls[-1]["messages"] if m.role != "system"]
        assert ("assistant", "ANSWER-ONE.") not in prompt
        assert prompt[-1] == ("user", "QUESTION-TWO")


PLAIN_RECORDS = [(1, 'user_input'), (2, 'llm_call'), (3, 'assistant_token'), (4, 'done'), (5, 'user_input'), (6, 'llm_call'), (7, 'assistant_token'), (8, 'done')]
PLAIN_SUBJECTS = ['turn[<sid>]: ANSWER-TWO.', 'user[<sid>]: QUESTION-TWO', 'turn[<sid>]: ANSWER-ONE.', 'user[<sid>]: QUESTION-ONE']
TOOL_RECORDS = [
    (1, "user_input"), (2, "tool_call"), (3, "llm_call"), (4, "done"), (5, "tool_result"), (6, "llm_call"),
    (7, "assistant_token"), (8, "done"),
]
TOOL_SUBJECTS = ['turn[<sid>]: DONE.', 'user[<sid>]: RUN IT']
COMPACTION_RECORDS = [(1, 'user_input'), (2, 'llm_call'), (3, 'assistant_token'), (4, 'done'), (5, 'user_input'), (6, 'compaction_marker'), (7, 'llm_call'), (8, 'assistant_token'), (9, 'done')]
COMPACTION_SUBJECTS = ['turn[<sid>]: A2.', '<sid>: compaction marker', 'user[<sid>]: Q2', 'turn[<sid>]: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa…', 'user[<sid>]: qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq…']
