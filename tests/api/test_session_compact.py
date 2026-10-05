"""REST tests for POST /v1/workspaces/{wid}/sessions/{sid}/compact.

S1 P2 Task 12 wrote the guards. The journey (the summariser, the marker, what the next load rebuilds, a steer
that lands during the call, the 422 for nothing to summarise) is covered here with a stub LLM wired into the
in-process app: ``tests/e2e/test_session_compact_journey.py`` has the same journey, but the e2e lane is skipped
unless ``PRIMER_RUN_E2E=1``, so these are the ones a plain unit run exercises.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest


def _now() -> datetime:
    return datetime(2026, 8, 16, 10, 0, 0, tzinfo=UTC)


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

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = f"{self.state_path}/sessions/{session_id}/messages.jsonl"
        self._files[path] = self._files.get(path, b"") + line


def _rec(seq, kind, **payload):
    return json.dumps({"seq": seq, "kind": kind, "payload": payload,
                       "created_at": "2026-08-16T00:00:00+00:00"})


_LOG = "\n".join([
    _rec(1, "user_input", text="hello"),
    _rec(2, "done"),
]) + "\n"


async def _seed(fake_storage_provider, sid, binding=None, **over):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    fields = {
        "id": sid, "workspace_id": "ws-1",
        "binding": binding or AgentSessionBinding(agent_id="ag1"),
        "status": SessionStatus.WAITING, "created_at": _now(),
        "turn_status": "idle", "last_seq": 2,
    }
    fields.update(over)
    await fake_storage_provider.get_storage(WorkspaceSession).create(
        WorkspaceSession(**fields)
    )


def _wire(app, ws):
    async def _get(wid):
        return ws if wid == "ws-1" else None

    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_running_turn_is_409(client, app, fake_storage_provider):
    await _seed(fake_storage_provider, "c-1", turn_status="running")
    ws = _FakeWorkspace()
    ws.write(".state/sessions/c-1/messages.jsonl", _LOG)
    _wire(app, ws)

    r = await client.post("/v1/workspaces/ws-1/sessions/c-1/compact")
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_parked_session_is_409(client, app, fake_storage_provider):
    """A park is mid-turn: its resume still needs the folded history."""
    await _seed(fake_storage_provider, "c-2", parked_status="parked")
    ws = _FakeWorkspace()
    ws.write(".state/sessions/c-2/messages.jsonl", _LOG)
    _wire(app, ws)

    r = await client.post("/v1/workspaces/ws-1/sessions/c-2/compact")
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_graph_binding_is_409(client, app, fake_storage_provider):
    """Graph internals see graph state, not session history."""
    from primer.model.workspace_session import GraphSessionBinding

    await _seed(
        fake_storage_provider, "c-3",
        binding=GraphSessionBinding(graph_id="g1"),
    )
    ws = _FakeWorkspace()
    ws.write(".state/sessions/c-3/messages.jsonl", _LOG)
    _wire(app, ws)

    r = await client.post("/v1/workspaces/ws-1/sessions/c-3/compact")
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_unknown_session_is_404(client, app, fake_storage_provider):
    ws = _FakeWorkspace()
    _wire(app, ws)
    r = await client.post("/v1/workspaces/ws-1/sessions/nope/compact")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# The journey, with a stub LLM
# ---------------------------------------------------------------------------

AGENT_ID = "ag-journey"
SYSTEM_PROMPT = "Be exact. " * 400  # 4,000 characters: about 1,000 tokens of fixed part


def _msg(role, text):
    return json.dumps({"role": role, "parts": [{"type": "text", "text": text}]})


_JOURNEY_LOG = [
    _rec(1, "user_input", text="hello"),
    _msg("user", "hello"),
    _rec(2, "assistant_token", text="hi back"),
    _msg("assistant", "hi back"),
    _rec(3, "done"),
]


class _StubLLM:
    def __init__(self, summary="rolled up"):
        self._summary = summary
        self.calls = []

    async def count_tokens(self, *args, **kwargs):
        return 10_000

    def stream(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": list(messages)})
        return self._stream_impl()

    async def _stream_impl(self):
        from primer.model.chat import Done, TextDelta

        yield TextDelta(index=0, text=self._summary)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self):
        return None


async def _journey(app, fake_storage_provider, monkeypatch, *, lines=None, llm=None, system_prompt=None):
    """A session with ``lines`` as its log, an agent, and a stub LLM behind the provider registry."""
    from primer.model.agent import Agent, AgentModel
    from primer.model_profile.resolver import ResolvedModel

    storage = fake_storage_provider.get_storage(Agent)
    if await storage.get(AGENT_ID) is None:
        await storage.create(Agent(
            id=AGENT_ID, description="compact journey", model=AgentModel(profile_id="p--m"), tools=[],
            system_prompt=[system_prompt] if system_prompt else [],
        ))
    lines = lines if lines is not None else _JOURNEY_LOG
    sid = "j-1"
    from primer.model.workspace_session import AgentSessionBinding

    await _seed(
        fake_storage_provider, sid, binding=AgentSessionBinding(agent_id=AGENT_ID),
        last_seq=max((json.loads(line).get("seq", 0) for line in lines), default=0),
    )
    ws = _FakeWorkspace()
    ws.write(f".state/sessions/{sid}/messages.jsonl", "\n".join(lines) + "\n")
    _wire(app, ws)
    llm = llm or _StubLLM()

    async def _get_llm(_provider_id):
        return llm

    app.state.provider_registry.get_llm = _get_llm  # type: ignore[assignment]

    async def _resolve(*_a, **_k):
        return ResolvedModel(
            profile_id="p--m", provider_id="prov", model_name="m", context_length=128_000, config={},
        )

    monkeypatch.setattr("primer.model_profile.resolve_model", _resolve, raising=False)
    return ws, llm


def _marker(ws, sid="j-1"):
    rows = [json.loads(line) for line in ws._files[f".state/sessions/{sid}/messages.jsonl"].decode().splitlines()]
    return [r for r in rows if r.get("kind") == "compaction_marker"]


@pytest.mark.asyncio
async def test_compaction_folds_the_history_the_next_turn_rebuilds(client, app, fake_storage_provider, monkeypatch):
    from primer.workspace.session import reconstruct_compacted_history

    ws, _llm = await _journey(app, fake_storage_provider, monkeypatch, llm=_StubLLM("the story so far"))
    r = await client.post("/v1/workspaces/ws-1/sessions/j-1/compact")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "the story so far" in body["summary"] and "earlier conversation compacted on" in body["summary"]
    lines = ws._files[".state/sessions/j-1/messages.jsonl"].decode().splitlines()
    texts = [p.text for m in reconstruct_compacted_history(lines) for p in m.parts]
    assert len(texts) == 1 and "the story so far" in texts[0], "the fold collapses the head to one row"
    assert b"hi back" in ws._files[".state/sessions/j-1/messages.jsonl"], "append-only: the rows are still on disk"


@pytest.mark.asyncio
async def test_a_steer_written_while_the_summariser_ran_survives_the_fold(client, app, fake_storage_provider, monkeypatch):
    """The router re-reads the history just before the write (``reload_history``): the summarising call takes
    seconds and a steer can land in that time. Without the wiring the marker folds it away."""
    from primer.workspace.session import reconstruct_compacted_history

    steer = (_msg("user", "A STEER THAT LANDED DURING THE SUMMARISER CALL") + "\n").encode()
    holder: dict = {}

    class _SteeringLLM(_StubLLM):
        def stream(self, *, model, messages, **kwargs):
            path = ".state/sessions/j-1/messages.jsonl"
            holder["ws"]._files[path] += steer
            return super().stream(model=model, messages=messages, **kwargs)

    ws, _ = await _journey(app, fake_storage_provider, monkeypatch, llm=_SteeringLLM("the story so far"))
    holder["ws"] = ws
    r = await client.post("/v1/workspaces/ws-1/sessions/j-1/compact")
    assert r.status_code == 200, r.text
    lines = ws._files[".state/sessions/j-1/messages.jsonl"].decode().splitlines()
    texts = [p.text for m in reconstruct_compacted_history(lines) for p in m.parts]
    assert "A STEER THAT LANDED DURING THE SUMMARISER CALL" in texts, "the steer was folded into the summary"


@pytest.mark.asyncio
async def test_nothing_to_summarise_is_a_422_that_names_the_reason(client, app, fake_storage_provider, monkeypatch):
    """One user message and no reply: that input is unanswered, so there is nothing to fold."""
    ws, llm = await _journey(
        app, fake_storage_provider, monkeypatch, lines=[_rec(1, "user_input", text="hello"), _msg("user", "hello")],
    )
    r = await client.post("/v1/workspaces/ws-1/sessions/j-1/compact")
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["extensions"]["reason"] == "empty_head" and "nothing to compact" in body["detail"]
    assert llm.calls == [], "no summariser call"
    assert _marker(ws) == []


@pytest.mark.asyncio
def _expected_fixed(system_prompt: str | None) -> int:
    """What the executor renders as the system prompt (the agent's own plus the workspace fragment), sized by the
    strategy's own estimate. The tool schemas are not part of it: the route has no executor to ask."""
    from primer.agent.compaction import CompactionStrategy
    from primer.agent.prompt_render import render_system_prompt_or_raw
    from primer.model.chat import Message, TextPart
    from primer.model.graph import build_execution_context
    from primer.workspace.session import workspace_system_prompt_fragment

    parts = [*([system_prompt] if system_prompt else []), workspace_system_prompt_fragment("j-1")]
    text = render_system_prompt_or_raw(parts, build_execution_context())
    return CompactionStrategy.estimate_fixed_overhead([Message(role="system", parts=[TextPart(text=text)])], [])


@pytest.mark.asyncio
async def test_the_markers_figures_count_what_the_route_can_see_of_the_fixed_part(
    client, app, fake_storage_provider, monkeypatch,
):
    """The route renders the system prompt the way the executor does (the agent's own plus the workspace fragment it
    appends; the tool schemas are behind the session's executor and are not counted here), so the marker's
    ``tokens_before`` / ``tokens_after`` include it and say how much. ``tokens_before`` used to be a flat 0: the route
    told the strategy the history was empty."""
    ws, _ = await _journey(app, fake_storage_provider, monkeypatch, system_prompt=SYSTEM_PROMPT)
    r = await client.post("/v1/workspaces/ws-1/sessions/j-1/compact")
    assert r.status_code == 200, r.text
    (marker,) = _marker(ws)
    payload = marker["payload"]
    assert payload["fixed_overhead_tokens"] == _expected_fixed(SYSTEM_PROMPT), "the agent's prompt and the fragment"
    assert payload["fixed_overhead_tokens"] > 1_000 + 250, "the 4,000 characters of the agent and the fragment"
    assert payload["tokens_before"] > payload["fixed_overhead_tokens"], "history and the fixed part"
    assert payload["tokens_after"] >= payload["fixed_overhead_tokens"]
    body = r.json()
    assert (body["tokens_before"], body["tokens_after"]) == (payload["tokens_before"], payload["tokens_after"])


@pytest.mark.asyncio
async def test_an_agent_without_a_system_prompt_still_has_the_workspace_fragment_as_its_fixed_part(
    client, app, fake_storage_provider, monkeypatch,
):
    ws, _ = await _journey(app, fake_storage_provider, monkeypatch)
    r = await client.post("/v1/workspaces/ws-1/sessions/j-1/compact")
    assert r.status_code == 200, r.text
    (marker,) = _marker(ws)
    fixed = marker["payload"]["fixed_overhead_tokens"]
    assert fixed == _expected_fixed(None) and 0 < fixed < 1_000, "the fragment every workspace executor appends"
    assert marker["payload"]["tokens_before"] > fixed


@pytest.mark.asyncio
async def test_a_summariser_that_overflows_is_retried_once_and_the_marker_says_so(client, app, fake_storage_provider, monkeypatch):
    """The manual route summarises through the same ``_full_compact`` as the turn path, so it gets the same one
    recovery: the first call is rejected as a context overflow, the second (text only, on a smaller input) answers,
    and the marker records what was cut. (The stub rejects the first call whatever its size, as a provider that
    counts more than we do would.)"""
    from primer.model.except_ import BadRequestError

    class _OverflowsOnce(_StubLLM):
        async def _stream_impl(self):
            if len(self.calls) == 1:
                raise BadRequestError("This model's maximum context length is 128000 tokens, however you requested more")
            async for event in super()._stream_impl():
                yield event

    big = [_msg("user", "q" * 80_000), _msg("assistant", "a" * 80_000)]            # ~40k tokens: a head worth cutting
    ws, llm = await _journey(app, fake_storage_provider, monkeypatch, llm=_OverflowsOnce("the story so far"),
                             lines=[_rec(1, "user_input", text="hello"), big[0], _rec(2, "assistant_token", text="hi"), big[1], _rec(3, "done")])
    r = await client.post("/v1/workspaces/ws-1/sessions/j-1/compact")
    assert r.status_code == 200, r.text
    sizes = [sum(len(p.text) for m in call["messages"] for p in m.parts) for call in llm.calls]
    rejected, retries = sizes[0], sizes[1:]
    assert retries and all(size < rejected for size in retries), "every retry call is sent less than the one the provider rejected"
    (marker,) = _marker(ws)
    reduced = marker["payload"]["summary_input_reduced"]
    assert reduced == {"pruned": 0, "folded_chunks": len(retries) if len(retries) > 1 else 0, "truncated_parts": 0}, reduced
    assert "the story so far" in marker["payload"]["summary"]


@pytest.mark.asyncio
async def test_a_marker_of_a_summariser_that_did_not_overflow_has_no_reduction(client, app, fake_storage_provider, monkeypatch):
    ws, _ = await _journey(app, fake_storage_provider, monkeypatch)
    r = await client.post("/v1/workspaces/ws-1/sessions/j-1/compact")
    assert r.status_code == 200, r.text
    (marker,) = _marker(ws)
    assert "summary_input_reduced" not in marker["payload"]

