"""GR-11: a resume failure whose error text carries a credential must not hand it to the model or the transcript.

The three "resume failed" syntheses (``session_resume_coordinator.py``:
``continuation resume failed`` / ``resume failed``, and
``yield_runtime.py``: ``call_tool resume failed``) embed ``str(exc)`` in a
tool result that goes to the model provider and the transcript. The
``ToolResultPart`` validator already masks a failed part's output (``tests/session/test_resume_tails_carry_no_credentials.py``
pins that), so this test is GREEN before the call-site fix: it pins the
end-to-end path for a resume hook that raises with a credential in its text,
and the fix (``redact_credentials`` at the three call sites, per the plan) is
defense-in-depth for the pre-validator text.
"""

from __future__ import annotations

import json

import pytest

from primer.model.chat import Message, ToolCallPart, ToolResultPart
from primer.model.workspace_session import WorkspaceSession
from tests.conftest import _FakeStorageProvider
from tests.worker.test_engine_session_resume import (
    _NoopPersist,
    _RecordingExecutor,
    _async_return,
    _build_engine,
    _build_pool,
    _claim_session,
    _make_resumable_session,
)

_LEAKY_HOOK_TOOL = "_test_engine_resume_hook_leaky"


def _leaky_hook(yield_metadata, event_payload, ctx):
    raise RuntimeError("GET https://u:p@h/x failed")


@pytest.fixture
def leaky_resume_hook(request):
    """Register ``_leaky_hook`` for the test and remove it when the test ends: the resume hook registry is process-wide, and a hook left in it
    would stay registered for every later test in the run."""
    from primer.worker import yield_resume_registry

    yield_resume_registry.register_resume_hook(_LEAKY_HOOK_TOOL, _leaky_hook)
    request.addfinalizer(lambda: yield_resume_registry._registry.pop(_LEAKY_HOOK_TOOL, None))


@pytest.mark.asyncio
async def test_resume_hook_error_does_not_leak_credentials_in_the_tool_result(monkeypatch, leaky_resume_hook):
    """GR-11: a resume hook whose error text carries a URL with userinfo must
    not reach the model or the transcript: the synthesized tool_result's
    reason is redacted (``redact_credentials`` at the call site; the
    ``ToolResultPart`` model masks error outputs too)."""
    sid = "sess-engine-resume-hook-leaky"
    tool_call_id = "tc-hook-leaky"
    assistant_msg = Message(
        role="assistant",
        parts=[ToolCallPart(id=tool_call_id, name="t__wait", arguments={})],
    )

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)
    await session_storage.create(_make_resumable_session(
        sid,
        tool_name=_LEAKY_HOOK_TOOL,
        tool_call_id=tool_call_id,
        resume_event_payload={"response": "x"},
        llm_messages=[assistant_msg.model_dump(mode="json")],
    ))
    fake_executor = _RecordingExecutor()
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _ws_id: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(fake_executor))

    await pool._run_engine_session(await _claim_session(engine, sid))

    (injected,) = fake_executor.injected
    tool_part = next(p for p in injected[-1].parts if isinstance(p, ToolResultPart))
    assert tool_part.id == tool_call_id and tool_part.error is True
    body = json.loads(tool_part.output)
    assert body["is_error"] is True
    assert "u:p@" not in body["reason"]
    assert body["reason"].startswith("resume failed: RuntimeError")
