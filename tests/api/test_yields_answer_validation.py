"""The ask_user answer's schema validation (Part 3 Task 109, GS-29).

Characterization: the 422 texts for ordinary schemas, pinned before the answer route moved to the bounded validator.
Then: the answer is validated through ``validate_bounded_off_loop``, so an agent-supplied pathological pattern answers 422
instead of stalling the API loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from primer.api.app import create_test_app
from primer.bus.in_memory import InMemoryEventBus
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession


def _parked(session_id: str, schema: dict | None) -> WorkspaceSession:
    now = datetime.now(timezone.utc)
    key = f"ask_user:{session_id}:tc-1"
    sess = WorkspaceSession(
        id=session_id, workspace_id="ws-x", binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING, created_at=now,
    )
    sess.parked_status = "parked"
    sess.parked_event_key = key
    sess.parked_until = now + timedelta(seconds=600)
    sess.parked_at = now
    sess.parked_state = {
        "schema_version": 1, "tool_call_id": "tc-1",
        "yielded": {"tool_name": "ask_user", "event_key": key, "timeout": 600.0,
                    "resume_metadata": {"prompt": "?", "response_schema": schema, "tool_call_id": "tc-1"}},
        "llm_messages": [], "turn_no": 1, "started_at": now.isoformat(), "resume_event_payload": None,
    }
    return sess


async def _answer(fake_storage_provider, fake_provider_registry, schema: dict | None, response) -> tuple[int, dict, str]:
    app = create_test_app(storage_provider=fake_storage_provider, provider_registry=fake_provider_registry)
    bus = InMemoryEventBus()
    await bus.initialize()
    app.state.event_bus = bus
    store = fake_storage_provider.get_storage(WorkspaceSession)
    await store.create(_parked("s-1", schema))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/v1/auth/register", json={"username": "admin", "password": "testpassword"})
            resp = await c.post("/v1/sessions/s-1/ask_user/respond", json={"tool_call_id": "tc-1", "response": response})
    finally:
        await bus.aclose()
    row = await store.get("s-1")
    return resp.status_code, resp.json(), row.parked_status


_OBJ = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

CASES = {
    "valid": (_OBJ, {"name": "Alice"}, 202, None),
    "required": (_OBJ, {"wrong": 1}, 422, "response failed schema validation: 'name' is a required property"),
    "type": (_OBJ, {"name": 3}, 422, "response failed schema validation: 3 is not of type 'string'"),
    "pattern": ({"type": "string", "pattern": "^[a-z]+$"}, "ABC", 422,
                "response failed schema validation: 'ABC' does not match '^[a-z]+$'"),
    "bad-schema": ({"type": "notatype"}, "x", 422, "response_schema is invalid: 'notatype' is not valid under any of the given schemas"),
    "bad-ref": ({"type": "object", "properties": {"name": {"$ref": "#/$defs/missing"}}}, {"name": "A"}, 422,
                "response_schema is invalid: its $ref '/$defs/missing' does not resolve "
                "(a $ref must point inside the schema; other documents are not fetched)"),
}


@pytest.mark.parametrize("case", sorted(CASES))
async def test_the_answer_verdict_is_unchanged(fake_storage_provider, fake_provider_registry, case: str) -> None:
    schema, response, status, detail = CASES[case]
    code, body, parked = await _answer(fake_storage_provider, fake_provider_registry, schema, response)
    assert code == status, body
    if detail is not None:
        assert body["detail"] == detail
        assert parked == "parked"


async def test_the_answer_is_validated_off_the_loop_and_bounded(fake_storage_provider, fake_provider_registry, monkeypatch) -> None:
    import primer.common.bounded_jsonschema as bounded

    seen: list[object] = []
    real = bounded.validate_bounded_off_loop

    async def spy(instance, schema, **kwargs):
        seen.append(instance)
        return await real(instance, schema, **kwargs)

    monkeypatch.setattr(bounded, "validate_bounded_off_loop", spy)
    code, _, _ = await _answer(fake_storage_provider, fake_provider_registry, _OBJ, {"name": "Alice"})
    assert code == 202
    assert seen == [{"name": "Alice"}]


async def test_a_pathological_pattern_answers_422(fake_storage_provider, fake_provider_registry) -> None:
    code, body, parked = await _answer(
        fake_storage_provider, fake_provider_registry, {"type": "string", "pattern": r"(a|aa)+$"}, "a" * 60 + "!",
    )
    assert code == 422, body
    assert body["detail"] == "response failed schema validation: its patterns used their 1 s matching budget"
    assert parked == "parked"


async def test_a_pattern_over_the_caps_answers_422(fake_storage_provider, fake_provider_registry) -> None:
    code, body, _ = await _answer(fake_storage_provider, fake_provider_registry, {"type": "string", "pattern": "x" * 1001}, "x")
    assert code == 422, body
    assert body["detail"].startswith("response_schema is invalid: ")
