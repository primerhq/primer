"""wait_for_event: park bookkeeping, resume formatting, full wake loop."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest_asyncio

from primer.events.dispatcher import EventDispatcher
from primer.model.event import EventSubscription
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolContext, Yielded, YieldTimeout
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.events_wait import (
    make_wait_for_event_handler,
    wait_for_event_resume,
)

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_key, payload=None):
        self.published.append((event_key, payload or {}))


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    try:
        yield provider
    finally:
        await provider.aclose()


def _ctx(session_id="sess-1", tool_call_id="call-1") -> ToolContext:
    return ToolContext(
        tool_call_id=tool_call_id,
        session_id=session_id,
        workspace_id="w",
    )


async def test_handler_creates_pinned_one_shot_and_yields(sp):
    store = sp.get_event_store()
    await store.append(event_type="session.steered")  # pre-existing history
    handler = make_wait_for_event_handler(sp)

    result = await handler(
        {"event_types": ["collection.document_pushed"],
         "timeout_seconds": 60.0},
        ctx=_ctx(),
    )
    assert isinstance(result, Yielded)
    assert result.event_key == "evwait:sess-1:call-1"
    assert result.timeout == 60.0

    sub_id = result.resume_metadata["subscription_id"]
    sub = await sp.get_storage(EventSubscription).get(sub_id)
    assert sub is not None
    assert sub.sink.kind == "session_wake"
    assert sub.sink.one_shot is True
    assert sub.filter.event_types == ["collection.document_pushed"]
    # Cursor pinned at head: pre-existing history is out of scope.
    assert await store.get_cursor(sub_id) == await store.max_id()


async def test_handler_rejects_chat_surface_and_bad_regex(sp):
    handler = make_wait_for_event_handler(sp)
    no_session = await handler(
        {"event_types": ["*"]},
        ctx=ToolContext(tool_call_id="c", session_id=None,
                        workspace_id=None, chat_id="chat-1"),
    )
    assert no_session.is_error

    bad_regex = await handler(
        {"event_types": ["*"],
         "fields": [{"path": "payload.x", "op": "regex",
                     "value": "([unclosed"}]},
        ctx=_ctx(),
    )
    assert bad_regex.is_error
    # Nothing half-created.
    from primer.model.storage import OffsetPage

    page = await sp.get_storage(EventSubscription).list(OffsetPage(length=10))
    assert page.items == []


async def test_resume_formats_event_timeout(sp):
    envelope = {"event_type": "collection.document_pushed", "id": 7}
    out = wait_for_event_resume({}, envelope, None)
    assert not out.is_error
    assert '"event"' in out.output

    timed = wait_for_event_resume(
        {"event_types": ["*"]}, YieldTimeout(elapsed_seconds=5.0), None,
    )
    assert not timed.is_error
    assert "timed_out" in timed.output


async def test_full_wake_loop_through_the_dispatcher(sp):
    """Handler-created subscription + parked row + dispatcher = wake."""
    handler = make_wait_for_event_handler(sp)
    yielded = await handler(
        {"event_types": ["collection.document_pushed"],
         "fields": [{"path": "payload.collection_id", "op": "eq",
                     "value": "kb"}]},
        ctx=_ctx(),
    )
    assert isinstance(yielded, Yielded)

    # The worker would write this park after the tool returns.
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="sess-1", workspace_id="w",
        binding=AgentSessionBinding(agent_id="agent-a"),
        status=SessionStatus.WAITING,
        created_at=datetime.now(timezone.utc),
        parked_status="parked",
        parked_event_key=yielded.event_key,
    ))

    bus = _Bus()
    dispatcher = EventDispatcher(storage_provider=sp, event_bus=bus)
    store = sp.get_event_store()

    # A non-matching collection does not wake it.
    await store.append(
        event_type="collection.document_pushed",
        payload={"collection_id": "other", "path": "p"},
    )
    assert await dispatcher.drain_once() == 0
    assert bus.published == []

    # The matching one does, delivering the envelope on the park key.
    await store.append(
        event_type="collection.document_pushed",
        payload={"collection_id": "kb", "path": "guides/x"},
    )
    assert await dispatcher.drain_once() == 1
    [(key, payload)] = bus.published
    assert key == yielded.event_key
    assert payload["payload"]["collection_id"] == "kb"
    sub_id = yielded.resume_metadata["subscription_id"]
    assert await sp.get_storage(EventSubscription).get(sub_id) is None


# --- SEC-04: a woken wait never hands the agent a stored secret ---------

_SECRETS = ("Bearer HDR-SECRET-1", "OAUTH-CLIENT-SECRET-2", "ENV-SECRET-3")


def _toolset_payloads() -> list[dict]:
    """Two toolset rows dumped the way storage writes CRUD event payloads
    (dump_for_storage unwraps SecretStr to plaintext)."""
    from pydantic import SecretStr

    from primer.model.common import dump_for_storage
    from primer.model.providers.toolset import (
        HttpConfig,
        McpConfig,
        OAuthClientCredentials,
        OAuthConfig,
        StdioConfig,
        Toolset,
        ToolsetProviderType,
        TransportType,
    )

    http = Toolset(
        id="toolset-http", provider=ToolsetProviderType.MCP,
        config=McpConfig(
            transport=TransportType.HTTP,
            config=HttpConfig(
                url="https://mcp.example.com/mcp",
                headers={"Authorization": SecretStr(_SECRETS[0])},
                oauth=OAuthConfig(
                    redirect_uri="https://primer.example.com/cb",
                    static_client=OAuthClientCredentials(
                        client_id="client-a",
                        client_secret=SecretStr(_SECRETS[1]),
                    ),
                ),
            ),
        ),
    )
    stdio = Toolset(
        id="toolset-stdio", provider=ToolsetProviderType.MCP,
        config=McpConfig(
            transport=TransportType.STDIO,
            config=StdioConfig(
                command=["mcp-server"],
                env={"UPSTREAM_VALUE": SecretStr(_SECRETS[2])},
            ),
        ),
    )
    return [dump_for_storage(http), dump_for_storage(stdio)]


def _assert_no_secret(text: str) -> None:
    for secret in _SECRETS:
        assert secret not in text, f"{secret!r} leaked: {text[:400]}"


async def test_resume_masks_toolset_secrets_in_the_event_payload():
    """The tool result is what lands in the agent's transcript."""
    for payload in _toolset_payloads():
        assert any(s in json.dumps(payload) for s in _SECRETS)  # premise
        envelope = {"id": 9, "event_type": "toolset.updated",
                    "entity_kind": "toolset", "payload": payload}
        out = wait_for_event_resume({}, envelope, None)
        assert not out.is_error
        _assert_no_secret(out.output)
        # The non-secret shape stays readable for the agent.
        assert payload["id"] in out.output


async def test_a_toolset_event_reaches_the_bus_and_the_agent_masked(sp):
    """End to end: a toolset CRUD event woken through the dispatcher
    carries no plaintext MCP header, OAuth client_secret or env value,
    on the wake bus or in the tool result."""
    handler = make_wait_for_event_handler(sp)
    yielded = await handler({"event_types": ["toolset.*"]}, ctx=_ctx())
    assert isinstance(yielded, Yielded)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="sess-1", workspace_id="w",
        binding=AgentSessionBinding(agent_id="agent-a"),
        status=SessionStatus.WAITING,
        created_at=datetime.now(timezone.utc),
        parked_status="parked",
        parked_event_key=yielded.event_key,
    ))
    bus = _Bus()
    dispatcher = EventDispatcher(storage_provider=sp, event_bus=bus)
    http_payload, _ = _toolset_payloads()
    await sp.get_event_store().append(
        event_type="toolset.updated", entity_kind="toolset",
        entity_id="toolset-http", payload=http_payload,
    )
    assert await dispatcher.drain_once() == 1
    [(_key, published)] = bus.published
    _assert_no_secret(json.dumps(published))
    http_cfg = published["payload"]["config"]["config"]
    assert http_cfg["url"] == "https://mcp.example.com/mcp"
    # Header NAMES are config, not secrets: they stay visible.
    assert "Authorization" in http_cfg["headers"]
    assert http_cfg["oauth"]["static_client"]["client_id"] == "client-a"
    out = wait_for_event_resume(yielded.resume_metadata, published, None)
    _assert_no_secret(out.output)


def test_secret_field_names_covers_model_subpackages():
    """The toolset model lives in primer.model.providers; its SecretStr
    fields (headers, env, client_secret) must be in the registry."""
    from primer.events.redaction import secret_field_names

    assert {"headers", "env", "client_secret"} <= secret_field_names()
