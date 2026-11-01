"""A tool that raises anything but a ``PrimerError`` is answered as a tool error and the turn goes on (W16, lead ruling D1).

``ToolExecutionManager`` answered only a ``PrimerError`` as an error result. Any other exception (a ``KeyError`` in a handler, a workspace tool's
``RuntimeError``, an ``OSError`` walking a symlink) left ``execute``: the in-process loop failed the turn, and the claims path retried it as transient up
to the attempt cap, running the earlier calls' side effects again. It is now an error result naming the tool, with a fixed text (the exception's own
may carry paths or secrets) and an ERROR log with the traceback. ``AuthRequiredError``, a park (``YieldToWorker``) and a routing ``PrimerError``
still leave ``execute`` as before, and ``CancelledError`` (a ``BaseException``) is never caught.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

import primer.workspace  # noqa: F401  (resolves ToolCallContext's forward reference)
from primer.agent.loop import _dispatch_tool_calls
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import Tool, ToolCallPart, ToolCallResult
from primer.model.except_ import AuthRequiredError, NotFoundError, UnsupportedContentError
from primer.model.principal import PrincipalRef
from primer.model.yield_ import Yielded, YieldToWorker

SECRET = "/home/someone/.ssh/id_rsa token=sk-leak-0123456789"


class _Provider:
    """One toolset: ``ok`` answers, ``boom`` raises whatever ``raises`` holds."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.calls: list[str] = []

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        for name in ("ok", "boom"):
            yield Tool(id=name, description=f"the {name} tool", toolset_id="t", args_schema={"type": "object", "properties": {}})

    async def call(self, *, tool_name: str, arguments: dict, principal: str | None = None, ctx: Any = None) -> ToolCallResult:
        self.calls.append(tool_name)
        if tool_name == "boom" and self.raises is not None:
            raise self.raises
        return ToolCallResult(output=f"ran {tool_name}", is_error=False)


def _manager(provider: _Provider) -> ToolExecutionManager:
    return ToolExecutionManager(
        toolset_providers={"t": provider},  # type: ignore[dict-item]
        initiated_by=PrincipalRef(type="user", id="u1", display="u1", role="user", source="local"),
    )


class _WsTool:
    id = "edit"
    description = "a workspace tool"
    examples: list = []
    requires_workspace_context = True

    def __init__(self, raises: BaseException | None = None, output: str = "edited") -> None:
        self.raises, self.output = raises, output

    def parameters(self):
        from pydantic import BaseModel

        class _Args(BaseModel):
            path: str = "a.txt"

        return _Args

    async def execute(self, args, ctx):
        from primer.workspace.tool import ToolResult

        if self.raises is not None:
            raise self.raises
        return ToolResult(output=self.output, metadata={}, truncated=False)


class _Session:
    workspace_id, session_id, agent_id = "ws-1", "sess-1", "agent-1"
    workspace_tools: list = []

    async def cache_output(self, text: str) -> str:
        return "/tmp/cache/1.txt"


def _ws_manager(tool: _WsTool) -> ToolExecutionManager:
    return ToolExecutionManager(workspace_tools={"edit": tool}, workspace_session=_Session())  # type: ignore[dict-item, arg-type]


def _assert_fixed_error(result, name: str) -> None:
    assert result.error is True
    assert name in result.output
    assert SECRET not in result.output and "sk-leak" not in result.output and "id_rsa" not in result.output, result.output


@pytest.mark.parametrize("exc", [RuntimeError(SECRET), KeyError(SECRET), OSError(SECRET), UnicodeDecodeError("utf-8", b"\xff", 0, 1, SECRET)])
async def test_a_toolset_tool_that_raises_is_a_fixed_error_result_logged_with_its_traceback(exc, caplog) -> None:
    provider = _Provider(raises=exc)
    caplog.set_level(logging.ERROR, logger="primer.agent.tool_manager")

    result = await _manager(provider).execute(ToolCallPart(id="c1", name="t__boom", arguments={}))

    assert result.id == "c1"
    _assert_fixed_error(result, "t__boom")
    logged = [r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]
    assert logged and logged[0].exc_info[1] is exc, "the operator gets the traceback"


async def test_a_workspace_tool_that_raises_is_a_fixed_error_result(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="primer.agent.tool_manager")

    result = await _ws_manager(_WsTool(raises=RuntimeError(SECRET))).execute(ToolCallPart(id="c1", name="workspace__edit", arguments={}))

    _assert_fixed_error(result, "workspace__edit")
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


async def test_the_turn_answers_the_failed_call_and_runs_the_next_one() -> None:
    provider = _Provider(raises=RuntimeError(SECRET))
    calls = [ToolCallPart(id="c1", name="t__boom", arguments={}), ToolCallPart(id="c2", name="t__ok", arguments={})]

    (message,) = await _dispatch_tool_calls(calls, tool_manager=_manager(provider), principal=None, actions_out=[])

    assert [(p.id, p.error) for p in message.parts] == [("c1", True), ("c2", False)]
    assert message.parts[1].output == "ran ok"
    assert provider.calls == ["boom", "ok"]


# ---- characterization: what already left execute, and what was already answered ------------------------------------------------------


async def test_characterization_a_primer_error_from_the_tool_is_answered_with_its_text() -> None:
    result = await _manager(_Provider(raises=NotFoundError("no such thing 'x'"))).execute(ToolCallPart(id="c", name="t__boom", arguments={}))

    assert (result.error, result.output) == (True, "no such thing 'x'")


async def test_characterization_a_workspace_primer_error_is_answered_with_its_text() -> None:
    result = await _ws_manager(_WsTool(raises=NotFoundError("file 'a.txt' not found"))).execute(ToolCallPart(id="c", name="workspace__edit", arguments={}))

    assert (result.error, result.output) == (True, "file 'a.txt' not found")


async def test_characterization_auth_required_and_a_park_still_leave_execute() -> None:
    with pytest.raises(AuthRequiredError):
        await _manager(_Provider(raises=AuthRequiredError("sign in", auth_url="https://x/oauth", state="s"))).execute(
            ToolCallPart(id="c", name="t__boom", arguments={}),
        )
    park = YieldToWorker(Yielded(tool_name="ask", event_key="k"), tool_call_id="c")
    with pytest.raises(YieldToWorker) as raised:
        await _manager(_Provider(raises=park)).execute(ToolCallPart(id="c", name="t__boom", arguments={}))
    assert raised.value is park


async def test_characterization_an_unknown_tool_still_raises() -> None:
    with pytest.raises(UnsupportedContentError):
        await _manager(_Provider()).execute(ToolCallPart(id="c", name="t__nope", arguments={}))


async def test_characterization_a_cancellation_is_never_answered() -> None:
    with pytest.raises(asyncio.CancelledError):
        await _manager(_Provider(raises=asyncio.CancelledError())).execute(ToolCallPart(id="c", name="t__boom", arguments={}))


async def test_characterization_successful_outputs_are_unchanged() -> None:
    result = await _manager(_Provider()).execute(ToolCallPart(id="c", name="t__ok", arguments={}))
    assert (result.error, result.output) == (False, "ran ok")

    small = "line\n" * 1999
    ws = await _ws_manager(_WsTool(output=small)).execute(ToolCallPart(id="c", name="workspace__edit", arguments={}))
    assert (ws.error, ws.output) == (False, small)
