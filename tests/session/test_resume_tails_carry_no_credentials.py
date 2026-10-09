"""The park and resume tails build a failed tool result outside ``ToolExecutionManager.execute``; none of them may carry a credential (security ticket 01a11fbc-d6de, #676 review round 1).

``ToolExecutionManager.execute`` masks the credentials in a failed call's text, but the resume coordinators build their error results themselves: the approved
``call_tool`` re-dispatch, the ``resume failed`` syntheses, the resume-hook error outputs (``mcp_task_resume``, ``python_tool_resume``). Those reach the model
(``inject_resume_messages``) and the served TOOL_RESULT record (a graph agent node's ``messages.jsonl`` is git-committed) raw. The choke point is therefore the
MODEL, not the call sites: a ``ToolResultPart`` with ``error=True`` masks the credentials in its ``output`` when it is built (every construction, a rehydrated
parked message and a ``ToolCallTask.result_state`` included), and ``WorkspaceMessageWriter.append`` masks an error TOOL_RESULT record written as a dict.

A SUCCESSFUL result is deliberately not touched: a presigned URL the user asked a tool for must come back intact.
"""

from __future__ import annotations

import json
import types
from datetime import UTC, datetime

import httpx
import pytest

from primer.model.chat import Message, ToolCallPart, ToolCallResult, ToolResultPart
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.persistence import WorkspaceMessageWriter
from primer.toolset.mcp import mcp_task_resume
from primer.worker.session_resume_coordinator import inject_resume_and_continue
from primer.worker.yield_resume_registry import ResumeContext
from primer.worker.yield_runtime import _resume_call_tool_dispatch, _resume_tool_approval

PASSWORD = "hunter2pw"
KEY = "SKSECRET123456"
BEARER = "sk-bearer-ABCDEFGH12345678"
LEAKY_URL = f"https://svc-user:{PASSWORD}@gateway.internal/v1/chat?api_key={KEY}&x=1"


def _clean(text: str) -> bool:
    return PASSWORD not in text and KEY not in text and BEARER not in text


class _Raising:
    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        raise httpx.ConnectError(f"[Errno 111] Connection refused for url '{LEAKY_URL}'")


class _ErrorResult:
    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        return ToolCallResult(output=f"Server error '500 Internal Server Error' for url '{LEAKY_URL}'", is_error=True)


class _Registry:
    def __init__(self, provider) -> None:
        self.provider = provider

    async def get_toolset(self, toolset_id):
        return self.provider


class _Manager:
    def __init__(self, provider) -> None:
        self._provider_registry = _Registry(provider)


_CALL = ToolCallPart(id="c1", name="http_request", arguments={})
_VIA = {"toolset_id": "remote_mcp", "principal": None}


# ---- the model choke point ---------------------------------------------------------------------------------------------------------------------


def test_a_failed_result_masks_its_credentials_when_it_is_built() -> None:
    part = ToolResultPart(id="c1", output=f"failed for {LEAKY_URL} with Authorization: Bearer {BEARER}", error=True)

    assert _clean(part.output) and "[REDACTED]" in part.output


def test_a_successful_result_is_not_touched() -> None:
    """A presigned URL (or any text with a token in it) a tool was asked for comes back intact."""
    text = f"here is your link {LEAKY_URL}"

    assert ToolResultPart(id="c1", output=text, error=False).output == text


def test_masking_a_failed_result_is_idempotent() -> None:
    once = ToolResultPart(id="c1", output=f"failed for {LEAKY_URL}", error=True)
    again = ToolResultPart.model_validate(once.model_dump())

    assert again.output == once.output


def test_a_parked_message_rehydrated_from_the_row_masks_a_failed_result() -> None:
    """``ParkedState.llm_messages`` are rehydrated with ``Message.model_validate``: a row written before this fix is masked on the way back in."""
    stored = {"role": "tool", "parts": [{"type": "tool_result", "id": "c1", "output": f"failed for {LEAKY_URL}", "error": True}]}

    rehydrated = Message.model_validate(stored)

    assert _clean(rehydrated.parts[0].output)


def test_a_failed_result_that_is_json_stays_json() -> None:
    envelope = json.dumps({"error": f"GET \"{LEAKY_URL}\" -> 401", "error_type": "provider-error"})

    out = ToolResultPart(id="c1", output=envelope, error=True).output

    assert _clean(out) and json.loads(out)["error_type"] == "provider-error"


# ---- the resume tails ----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [_Raising(), _ErrorResult()], ids=["inner-raises", "inner-is-error"])
async def test_the_approved_call_tool_redispatch_carries_no_credential(provider) -> None:
    part = await _resume_call_tool_dispatch(via=_VIA, original_call=_CALL, tool_manager=_Manager(provider))

    assert part.error is True and _clean(part.output)


@pytest.mark.asyncio
async def test_the_session_resumes_approval_branch_carries_no_credential() -> None:
    blob = {"yielded": {"resume_metadata": {"original_call": {"id": "c1", "name": "http_request", "arguments": {}}, "via_call_tool": _VIA}}}

    part = await _resume_tool_approval(blob=blob, payload={"decision": "approved"}, tool_manager=_Manager(_ErrorResult()))

    assert part.error is True and _clean(part.output)


def test_a_failed_mcp_task_resume_hook_output_carries_no_credential() -> None:
    hook = mcp_task_resume(
        {"task_id": "t1"},
        {"result": {"isError": True, "content": [{"type": "text", "text": f"fetch failed for {LEAKY_URL}"}]}},
        ResumeContext(tool_name="__mcp_task__", tool_call_id="c1", session_id="s1", resolve_provider=None),
    )

    part = ToolResultPart(id="c1", output=hook.output, error=hook.is_error)    # what session_resume_coordinator builds from a hook's outcome

    assert part.error is True and _clean(part.output)


@pytest.mark.parametrize("synth", ["resume failed", "continuation resume failed"])
def test_the_resume_failed_syntheses_carry_no_credential(synth) -> None:
    output = json.dumps({"rejected": True, "reason": f"{synth}: ConnectError: refused for url '{LEAKY_URL}'", "tool_name": "http_request"})

    part = ToolResultPart(id="c1", output=output, error=True)

    assert _clean(part.output)


class _Workspace:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.append(line)

    async def append_state_line(self, *_a, **_k) -> None:
        return None


def _record(payload: dict) -> SessionMessageRecord:
    return SessionMessageRecord(seq=1, kind=SessionMessageKind.TOOL_RESULT, payload=payload, created_at=datetime.now(UTC))


@pytest.mark.asyncio
async def test_an_error_tool_result_record_written_as_a_dict_carries_no_credential() -> None:
    """``tool_wait_resume_coordinator`` and ``session/abandon`` write their TOOL_RESULT records as dicts through the writer, never through a part."""
    ws = _Workspace()
    writer = WorkspaceMessageWriter(workspace_io=ws, session_id="s1")

    await writer.append(_record({"call_id": "c1", "output": f"failed for {LEAKY_URL}", "error": True}))
    await writer.flush()

    assert _clean(b"".join(ws.lines).decode())


@pytest.mark.asyncio
async def test_a_successful_tool_result_record_is_written_as_it_was() -> None:
    ws = _Workspace()
    writer = WorkspaceMessageWriter(workspace_io=ws, session_id="s1")

    await writer.append(_record({"call_id": "c1", "output": f"link {LEAKY_URL}", "error": False}))
    await writer.flush()

    assert PASSWORD in b"".join(ws.lines).decode()


@pytest.mark.asyncio
async def test_the_shared_resume_tail_hands_the_model_and_the_record_a_clean_result() -> None:
    lines: list[bytes] = []
    injected: list[Message] = []

    class WorkspaceIO:
        async def append_message_line(self, session_id, line):
            lines.append(line)

        async def append_state_line(self, *_a, **_k):
            return None

    class Storage:
        async def update(self, row):
            return row

    class StorageProvider:
        def get_storage(self, model):
            return Storage()

    class Executor:
        async def inject_resume_messages(self, messages):
            injected.extend(messages)

    async def load_workspace(workspace_id):
        return WorkspaceIO()

    pool = types.SimpleNamespace(_storage=StorageProvider(), _load_workspace_for_persist=load_workspace, _event_bus=None)
    session = types.SimpleNamespace(id="s1", workspace_id="w1", last_seq=0, model_copy=lambda update: None)
    parked = types.SimpleNamespace(llm_messages=[], scoped_tool_call_id="c1#1")
    part = await _resume_call_tool_dispatch(via=_VIA, original_call=_CALL, tool_manager=_Manager(_ErrorResult()))

    await inject_resume_and_continue(pool, session, Executor(), parked, part)

    context = " ".join(p.output for m in injected for p in m.parts if isinstance(p, ToolResultPart))
    assert context and _clean(context)
    assert lines and _clean(b"".join(lines).decode())
