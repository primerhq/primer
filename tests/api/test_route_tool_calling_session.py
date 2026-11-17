"""Calling-session provenance crosses generated dispatch, never HTTP input."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Header, Request
from pydantic import BaseModel

from primer.access.person import Person
from primer.api import route_tools
from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext


class _Payload(BaseModel):
    session_id: str = "body-target"
    parent_session_id: str = "body-parent"


def _ctx(session_id):
    return ToolContext(
        tool_call_id="call", session_id=session_id, workspace_id="workspace",
        initiated_by=PrincipalRef.system(),
    )


def _probe(hook=None):
    app = FastAPI()

    @app.post("/probe/{session_id}")
    @route_tools.route_tool(scoped_id="caller_probe__read")
    async def probe(
        request: Request, session_id: str, body: _Payload,
        parent_session_id: str = "query-parent",
        claimed_session: str | None = Header(default=None, alias="primer.route_tool_session_id"),
    ):
        before = route_tools.calling_session_id(request)
        if hook is not None:
            await hook(request)
        person = route_tools.trusted_route_tool_person(request.scope)
        return {
            "caller": before, "after": route_tools.calling_session_id(request),
            "target": session_id, "body": body.model_dump(), "parent": parent_session_id,
            "header": claimed_session, "role": person.role if person else None,
        }

    snapshot = route_tools.build_generated_route_toolsets(app)
    return app, snapshot.providers["caller_probe"]


async def _call(provider, ctx=None, **arguments):
    result = await provider.call(tool_name="read", arguments={"session_id": "path-target", "body": {}, **arguments}, ctx=ctx)
    return result, json.loads(result.output)


@pytest.mark.parametrize("session_id", ["real-caller", None])
async def test_generated_call_uses_context_not_arguments(session_id):
    app, provider = _probe()
    assert app is not None  # Keep the app alive for the weakly bound provider.
    result, data = await _call(provider, _ctx(session_id), parent_session_id="chosen-parent", **{
        "primer.route_tool_session_id": "header-caller",
        "body": {"session_id": "body-caller", "parent_session_id": "body-parent"},
    })
    assert not result.is_error
    assert data == {
        "caller": session_id, "after": session_id, "target": "path-target",
        "body": {"session_id": "body-caller", "parent_session_id": "body-parent"},
        "parent": "chosen-parent", "header": "header-caller", "role": "system",
    }


async def test_direct_mcp_style_absent_context_does_not_infer_a_caller():
    app, provider = _probe()
    assert app is not None
    result, data = await _call(provider, parent_session_id="parent-is-not-caller")
    assert not result.is_error and data["caller"] is None and data["after"] is None


async def test_external_http_cannot_forge_scope_marker_or_session():
    app, _ = _probe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/probe/path-caller?parent_session_id=query-caller", json={
            "session_id": "body-caller", "parent_session_id": "parent-caller",
            "primer.route_tool_session_id": "body-scope",
        }, headers={
            "primer.route_tool_session_id": "header-caller",
            "primer.route_tool_dispatch": "true", "x-primer-session-id": "alternate-header",
        })
    assert response.status_code == 200
    data = response.json()
    assert data["caller"] is None and data["after"] is None and data["role"] is None
    assert data["target"] == "path-caller" and data["header"] == "header-caller"


@pytest.mark.parametrize("marker", [None, True, "true", {}, object()])
def test_serialized_or_foreign_scope_marker_is_not_trusted(marker):
    request = Request({"type": "http", "primer.route_tool_dispatch": marker, "primer.route_tool_session_id": "forged"})
    assert route_tools.calling_session_id(request) is None


@pytest.mark.parametrize("session_id", [None, "caller"])
async def test_wrapper_copies_scope_and_overwrites_stale_session(session_id):
    seen = []

    async def inner(scope, receive, send):
        seen.append((scope, route_tools.calling_session_id(Request(scope))))

    original = {"type": "http", "primer.route_tool_session_id": "stale"}
    person = Person("user", "user")
    wrapper = route_tools._TrustedAsgiApp(inner, person, session_id=session_id)
    await wrapper(original, None, None)
    assert original == {"type": "http", "primer.route_tool_session_id": "stale"}
    assert len(seen) == 1 and seen[0][0] is not original and seen[0][1] == session_id
    assert route_tools.trusted_route_tool_person(seen[0][0]) is person


async def test_legacy_wrapper_constructor_has_no_calling_session():
    seen = []

    async def inner(scope, receive, send):
        seen.append(route_tools.calling_session_id(Request(scope)))

    await route_tools._TrustedAsgiApp(inner, Person(None, "system"))({"type": "http"}, None, None)
    assert seen == [None]


async def test_concurrent_generated_and_external_requests_keep_separate_callers():
    entered = []
    all_entered = asyncio.Event()

    async def hook(request):
        entered.append(route_tools.calling_session_id(request))
        if len(entered) == 4:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), 10)

    app, provider = _probe(hook)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        results = await asyncio.gather(
            _call(provider, _ctx("one")), _call(provider, _ctx("two")), _call(provider),
            client.post("/probe/external", json={}),
        )
    assert sorted(x for x in entered if x is not None) == ["one", "two"] and entered.count(None) == 2
    for (result, data), expected in zip(results[:3], ["one", "two", None], strict=True):
        assert not result.is_error and data["caller"] == data["after"] == expected
    assert results[3].status_code == 200 and results[3].json()["caller"] is None


async def test_recursive_generated_call_stays_refused_without_replacing_outer_caller():
    nested = []

    async def hook(request):
        before = route_tools.calling_session_id(request)
        result, data = await _call(provider, _ctx("nested"))
        nested.append((before, route_tools.calling_session_id(request), result.is_error, data))

    app, provider = _probe(hook)
    assert app is not None
    result, data = await _call(provider, _ctx("outer"))
    assert not result.is_error and data["caller"] == data["after"] == "outer"
    assert len(nested) == 1 and nested[0][:3] == ("outer", "outer", True)
    assert nested[0][3]["type"] == "route-tool-recursion"


@pytest.mark.parametrize("termination", ["cancel", "error"])
async def test_aborted_request_does_not_leak_caller_to_next_dispatch(termination):
    entered = asyncio.Event()
    cleanup = []
    abort = True

    async def hook(request):
        if not abort:
            return
        try:
            entered.set()
            if termination == "error":
                raise RuntimeError("test failure")
            await asyncio.Event().wait()
        finally:
            cleanup.append(route_tools.calling_session_id(request))

    app, provider = _probe(hook)
    task = asyncio.create_task(_call(provider, _ctx("aborted")))
    await asyncio.wait_for(entered.wait(), 10)
    if termination == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result, _ = await task
        assert result.is_error
    assert cleanup == ["aborted"]
    abort = False
    for ctx, expected in [(None, None), (_ctx("next"), "next")]:
        result, data = await _call(provider, ctx)
        assert not result.is_error and data["caller"] == data["after"] == expected
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/probe/external", json={})
    assert response.status_code == 200 and response.json()["caller"] is None
