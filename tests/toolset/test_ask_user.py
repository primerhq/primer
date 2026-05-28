"""Unit tests for the ``ask_user`` yielding tool.

Mirrors the structure of the sleep migration tests in
``tests/toolset/test_yield_protocol.py``: a direct handler call to
verify the Yielded sentinel is shaped right, plus resume-hook tests
that cover the real-response / timeout / cancelled branches.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.yield_ import (
    ToolContext,
    YieldCancelled,
    YieldTimeout,
    Yielded,
)
from primer.toolset.misc import build_misc_toolset


@pytest.fixture
def misc():
    return build_misc_toolset()


@pytest.mark.asyncio
class TestAskUserHandler:
    """The handler builds a Yielded sentinel, doesn't block."""

    async def test_handler_returns_yielded_with_event_key(self, misc):
        # Direct handler call via the provider — this is the path the
        # worker takes; provider stamps tool_name and raises.
        from primer.model.yield_ import YieldToWorker

        ctx = ToolContext(
            tool_call_id="tc-abc",
            session_id="sess-1",
            workspace_id="ws-1",
        )
        with pytest.raises(YieldToWorker) as exc_info:
            await misc.call(
                tool_name="ask_user",
                arguments={"prompt": "What is your name?"},
                ctx=ctx,
            )
        y = exc_info.value.yielded
        assert isinstance(y, Yielded)
        assert y.tool_name == "ask_user"
        assert y.event_key == "ask_user:sess-1:tc-abc"
        assert y.timeout is None  # no explicit timeout → global cap
        assert y.resume_metadata["prompt"] == "What is your name?"
        assert y.resume_metadata["response_schema"] is None
        assert y.resume_metadata["tool_call_id"] == "tc-abc"

    async def test_handler_honours_explicit_timeout(self, misc):
        from primer.model.yield_ import YieldToWorker

        ctx = ToolContext(
            tool_call_id="tc-t",
            session_id="sess-t",
            workspace_id=None,
        )
        with pytest.raises(YieldToWorker) as exc_info:
            await misc.call(
                tool_name="ask_user",
                arguments={"prompt": "ok?", "timeout_seconds": 90.0},
                ctx=ctx,
            )
        assert exc_info.value.yielded.timeout == 90.0

    async def test_handler_persists_response_schema(self, misc):
        from primer.model.yield_ import YieldToWorker

        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        ctx = ToolContext(
            tool_call_id="tc-s",
            session_id="sess-s",
            workspace_id=None,
        )
        with pytest.raises(YieldToWorker) as exc_info:
            await misc.call(
                tool_name="ask_user",
                arguments={
                    "prompt": "Provide x",
                    "response_schema": schema,
                },
                ctx=ctx,
            )
        assert exc_info.value.yielded.resume_metadata["response_schema"] == schema

    async def test_handler_requires_session_id_in_ctx(self, misc):
        # Without session_id we can't form a unique event_key — the
        # tool fails loudly so future scopes don't accidentally lose
        # uniqueness across sessions.
        ctx = ToolContext(
            tool_call_id="tc-x",
            session_id=None,
            workspace_id=None,
        )
        result = await misc.call(
            tool_name="ask_user",
            arguments={"prompt": "?"},
            ctx=ctx,
        )
        # Returns a validation-error ToolCallResult, not Yielded.
        assert result.is_error is True
        body = json.loads(result.output)
        assert body["type"] == "bad-request"
        assert "session_id" in body["message"]

    async def test_handler_rejects_empty_prompt(self, misc):
        ctx = ToolContext(
            tool_call_id="tc-x",
            session_id="sess-1",
            workspace_id=None,
        )
        result = await misc.call(
            tool_name="ask_user",
            arguments={"prompt": ""},
            ctx=ctx,
        )
        assert result.is_error is True


@pytest.mark.asyncio
class TestAskUserResumeHook:
    """The resume hook produces the right ToolCallResult per payload."""

    async def test_resume_with_real_response(self):
        from primer.toolset.misc import ask_user_resume

        meta = {
            "prompt": "What is your name?",
            "response_schema": None,
            "tool_call_id": "tc-abc",
            "parked_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        result = ask_user_resume(meta, {"response": "Alice"})
        assert result.is_error is False
        body = json.loads(result.output)
        assert body["response"] == "Alice"

    async def test_resume_with_complex_response_value(self):
        # response may be any JSON-serialisable value (object, array, etc.)
        from primer.toolset.misc import ask_user_resume

        meta = {
            "prompt": "Provide config",
            "response_schema": {"type": "object"},
            "tool_call_id": "tc-c",
            "parked_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"response": {"foo": "bar", "n": 7}}
        result = ask_user_resume(meta, payload)
        body = json.loads(result.output)
        assert body["response"] == {"foo": "bar", "n": 7}

    async def test_resume_with_timeout(self):
        from primer.toolset.misc import ask_user_resume

        meta = {
            "prompt": "?",
            "response_schema": None,
            "tool_call_id": "tc-t",
            "parked_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        result = ask_user_resume(meta, YieldTimeout(elapsed_seconds=42.5))
        body = json.loads(result.output)
        assert body["timed_out"] is True
        assert body["elapsed_seconds"] == 42.5

    async def test_resume_with_cancelled(self):
        from primer.toolset.misc import ask_user_resume

        meta = {
            "prompt": "?",
            "response_schema": None,
            "tool_call_id": "tc-c",
            "parked_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        result = ask_user_resume(
            meta,
            YieldCancelled(
                reason="operator skipped",
                cancelled_at=datetime.now(timezone.utc),
                elapsed_seconds=1.2,
            ),
        )
        body = json.loads(result.output)
        assert body["cancelled"] is True
        assert body["reason"] == "operator skipped"
        assert body["elapsed_seconds"] == 1.2


@pytest.mark.asyncio
async def test_ask_user_tool_is_registered():
    """The tool is exposed via build_misc_toolset's tool catalog."""
    misc = build_misc_toolset()
    tool_names = {t.id async for t in misc.list_tools()}
    assert "ask_user" in tool_names


def test_ask_user_resume_hook_is_registered():
    """Import-time registration lets the worker look the hook up by name."""
    # Importing misc triggers register_resume_hook("ask_user", ...).
    import primer.toolset.misc  # noqa: F401
    from primer.worker.yield_resume_registry import get_resume_hook

    hook = get_resume_hook("ask_user")
    assert callable(hook)
