"""The MCP task bridge publishes no credential in an upstream task's failure text (security ticket 01a1201c-8918, item 3; #676 security review).

The bridge turns a terminal upstream task into a ``session.wake`` payload. A FAILED task's ``statusMessage`` is whatever the remote server (or the library
under it) printed, and the bridge put it in the payload raw: the payload is persisted in the platform event log (``wake_payload``) and served by
``GET /v1/events`` (``session.*`` is user-safe and the event redaction masks by key name only), and it reaches the model as the tool result. The bridge masks
the text where it builds the payload (URL credentials, Bearer and Basic tokens). A task that COMPLETED with data is data and is published as it was; one that
completed with ``isError`` carries an error text and is masked like a failure.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import mcp.types as mcp_types
import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.bus.mcp_tasks import McpTaskBridge
from primer.scheduler.in_memory import InMemoryScheduler, _LeaseState
from tests.conftest import _FakeStorageProvider
from tests.toolset.test_mcp_tasks import (
    _FakeSession,
    _FakeStdioProvider,
    _make_mcp_task_parked_session,
    _MockProviderRegistry,
)

LEAKY = (
    "upstream said: GET 'https://svc-user:hunter2pw@gateway.internal/v1/x?api_key=SKSECRET123456' -> 401 "
    "(Authorization: Bearer sk-abcdefgh12345678 / Basic dXNlcjpwYXNzd29yZA==)"
)
SECRETS = ("hunter2pw", "SKSECRET123456", "sk-abcdefgh12345678", "dXNlcjpwYXNzd29yZA==")


class _Session(_FakeSession):
    """An MCP session whose task ends as told: status, the failure message, and (for ``completed``) the result."""

    def __init__(self, *, status: str, status_message: str | None = None, result: dict | None = None) -> None:
        super().__init__(tools=[], task_id="t-abc")
        self._status, self._status_message, self._result = status, status_message, result

    async def send_request(self, request, result_type, **kwargs):
        method = getattr(request, "method", None)
        if method == "tasks/get":
            now = datetime.now(timezone.utc).isoformat()
            return mcp_types.GetTaskResult(
                taskId=request.params.task_id, status=self._status, statusMessage=self._status_message,
                createdAt=now, lastUpdatedAt=now, ttl=60_000,
            )
        if method == "tasks/result":
            return mcp_types.CallToolResult.model_validate(self._result)
        raise AssertionError(f"unexpected send_request method: {method!r}")


async def _run(session) -> tuple[dict, list]:
    """Run the bridge until it publishes once; return the bus payload and the ``session.wake`` events recorded in the platform log."""
    bus = InMemoryEventBus()
    await bus.initialize()
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    await scheduler.register_worker(worker_id="wrk", host="h", pid=1, capacity=1)
    scheduler._sessions["sess-1"] = _make_mcp_task_parked_session(session_id="sess-1", tool_call_id="tc-1", toolset_id="srv-1", task_id="t-abc")
    scheduler._leases["sess-1"] = _LeaseState(worker_id=None, expires_at=None, runnable=False, next_attempt_at=datetime.now(timezone.utc))
    sp = _FakeStorageProvider()
    store = sp.get_event_store()
    await store.ensure_schema()
    sub = bus.subscribe()
    bridge = McpTaskBridge(
        bus=bus, scheduler=scheduler, provider_registry=_MockProviderRegistry({"srv-1": _FakeStdioProvider(session, toolset_id="srv-1")}),
        poll_seconds=0.05, storage_provider=sp,
    )
    bridge.start()
    try:
        async def _wake():
            async for event in sub:
                if event.event_key == "mcp_task:srv-1:t-abc":      # the recorder also bumps its own "events appended" key on the bus
                    return event

        event = await asyncio.wait_for(_wake(), timeout=2.0)
        recorded = await store.read_after(0, event_type_prefix="session.wake")
        return event.payload, recorded
    finally:
        await sub.aclose()
        await bridge.stop()
        await scheduler.aclose()
        await bus.aclose()


def _text_of(payload: dict) -> str:
    return " ".join(part.get("text", "") for part in payload["result"]["content"])


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked in {text!r}"


@pytest.mark.asyncio
async def test_a_failed_tasks_message_is_published_and_persisted_without_credentials() -> None:
    payload, recorded = await _run(_Session(status="failed", status_message=LEAKY))

    assert payload["result"]["isError"] is True
    _assert_clean(_text_of(payload))
    assert "gateway.internal" in _text_of(payload) and "401" in _text_of(payload), "what failed is still said"
    [event] = recorded
    _assert_clean(str(event.payload["wake_payload"]))


@pytest.mark.asyncio
async def test_a_failed_task_without_a_message_keeps_the_default_text() -> None:
    payload, _recorded = await _run(_Session(status="failed"))

    assert _text_of(payload) == "task t-abc failed"


@pytest.mark.asyncio
async def test_a_task_that_completed_with_an_error_result_is_masked() -> None:
    result = {"content": [{"type": "text", "text": f"tool failed: {LEAKY}"}], "isError": True}

    payload, recorded = await _run(_Session(status="completed", result=result))

    _assert_clean(_text_of(payload))
    _assert_clean(str(recorded[0].payload["wake_payload"]))
    assert "gateway.internal" in _text_of(payload)


@pytest.mark.asyncio
async def test_an_error_result_is_masked_in_every_part_it_carries() -> None:
    """The whole error result is the upstream's text, not only its text parts: an embedded resource's ``text`` and ``structuredContent`` are persisted and read by
    the model like the rest. The parts without a credential, and the fields that are not text, come back as they were."""
    result = {
        "isError": True,
        "content": [
            {"type": "text", "text": f"first: {LEAKY}"},
            {"type": "text", "text": "second part, no credential"},
            {"type": "resource", "resource": {"uri": "file:///run.log", "mimeType": "text/plain", "text": f"log: {LEAKY}"}},
            {"type": "text", "text": "last: Authorization: Bearer sk-abcdefgh12345678"},
        ],
        "structuredContent": {
            "error": LEAKY,
            "attempts": [{"url": "https://svc-user:hunter2pw@gateway.internal/v1/x?api_key=SKSECRET123456", "status": 401}],
        },
    }

    payload, recorded = await _run(_Session(status="completed", result=result))

    _assert_clean(json.dumps(payload))
    _assert_clean(str(recorded[0].payload["wake_payload"]))
    published = payload["result"]
    assert [part["type"] for part in published["content"]] == ["text", "text", "resource", "text"]
    assert published["content"][1]["text"] == "second part, no credential"
    assert published["content"][2]["resource"]["uri"] == "file:///run.log"
    assert published["content"][2]["resource"]["mimeType"] == "text/plain"
    assert published["structuredContent"]["attempts"][0]["status"] == 401
    assert "gateway.internal" in published["structuredContent"]["error"], "what failed is still said"


@pytest.mark.asyncio
async def test_a_task_that_completed_with_data_is_published_as_it_was() -> None:
    """Successful data is not touched: a URL the tool was asked to return comes back intact."""
    result = {"content": [{"type": "text", "text": f"fetched: {LEAKY}"}], "structuredContent": {"page": LEAKY}, "isError": False}

    payload, _recorded = await _run(_Session(status="completed", result=result))

    assert payload["result"]["content"][0]["text"] == f"fetched: {LEAKY}"
    assert payload["result"]["structuredContent"] == {"page": LEAKY}


@pytest.mark.asyncio
async def test_a_failed_message_without_a_credential_is_published_as_it_was() -> None:
    payload, _recorded = await _run(_Session(status="failed", status_message="the build ran out of disk space"))

    assert _text_of(payload) == "the build ran out of disk space"
