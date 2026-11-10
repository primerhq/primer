"""The multi-user access matrix over the live API, REST and MCP side by side (access control, Phase 6 Task 5).

Users: the e2e admin, A (owner), B (the other user) and C (starts restricted). For every owned kind A creates one row and each check runs through BOTH doors
with one helper: B's REST client and B's MCP client (a bearer token with the ``mcp`` scope calling the matching built-in tool):

* no access: the row answers like a missing one (REST 404, MCP not-found) and is absent from the list;
* read: get works, update is refused (403 / forbidden);
* full: update works, delete is refused;
* owner transfer: the new owner deletes;
* public: C, once promoted from restricted, reads it; restricted C reads nothing.

The grantable infrastructure (a model profile and the six providers) has use grants only: an admin sets them, the grantee reads their own level, nobody else sees the view.

Then the inherited rows follow their parent (a document its collection, a subscription its trigger), the grantable infrastructure (an agent on a private
profile is refused for B until the admin grants use), the event attribution (A's create is A's, B reads no event of A's private workspace) and the
lifecycle (the admin deletes A: A's sole-owned rows are the admin's, B's grants on them survive; the admin demotes B: B reads nothing, promotion restores it).
Collect-only locally; CI runs it against the live server.

Nothing here depends on what the server happens to hold: the ``infra`` fixture has the admin create (through the public REST API) one row of
every grantable kind, and publishes the ones a user needs in order to make a row of an owned kind (a model profile and its LLM provider for an
agent, a local workspace provider and a template for a template and a workspace, a Slack channel provider for a channel).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from collections.abc import Callable

import httpx
import pytest
import pytest_asyncio

from tests._support.model_profiles import profile_id_for, seed_llm_provider
from tests.e2e.conftest import pgvector_ssp_body

pytestmark = pytest.mark.asyncio

_PASSWORD = "access-matrix-password-123"
OK, MISSING, DENIED = "ok", "missing", "denied"


# ---- people ----------------------------------------------------------------------------------------------------------------------------------


@asynccontextmanager
async def _user(admin: httpx.AsyncClient, base_url: str, username: str, *, role: str = "user") -> AsyncIterator[tuple[str, httpx.AsyncClient]]:
    made = await admin.post("/v1/admin/users", json={"username": username, "password": _PASSWORD, "role": role})
    assert made.status_code == 201, made.text
    user_id = made.json()["id"]
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0)) as c:
            login = await c.post("/v1/auth/login", json={"username": username, "password": _PASSWORD})
            assert login.status_code == 200, login.text
            yield user_id, c
    finally:
        await admin.delete(f"/v1/admin/users/{user_id}")


async def _set_role(admin: httpx.AsyncClient, user_id: str, role: str) -> None:
    r = await admin.patch(f"/v1/admin/users/{user_id}", json={"role": role})
    assert r.status_code == 200, r.text


async def _share(owner: httpx.AsyncClient, plural: str, entity_id: str, owner_id: str, grants: dict[str, str], **extra: Any) -> dict:
    """Replace the sharing of one row from its current view (owners kept as ``owner_id`` unless ``owners`` is given)."""
    view = (await owner.get(f"/v1/{plural}/{entity_id}/access")).json()
    body = {"owners": [owner_id], "grants": grants, "public": False, "public_tools": [], "rev": view["rev"], **extra}
    put = await owner.put(f"/v1/{plural}/{entity_id}/access", json=body)
    assert put.status_code == 200, put.text
    return put.json()


# ---- the infrastructure the owned kinds are made from ---------------------------------------------------------------------------------------------


@dataclass
class Infra:
    """The ids of one row of every grantable kind, created by the admin for this test."""

    llm_provider: str
    profile: str
    ws_provider: str
    template: str
    channel_provider: str
    embedding: str
    cross_encoder: str
    ssp: str


async def _publish(admin: httpx.AsyncClient, plural: str, rid: str) -> None:
    """Make an infrastructure row usable by every full user (a new one is private to admins)."""
    view = (await admin.get(f"/v1/{plural}/{rid}/access")).json()
    put = await admin.put(
        f"/v1/{plural}/{rid}/access",
        json={"owners": [o["id"] for o in view["owners"]], "grants": {g["user"]["id"]: g["level"] for g in view["grants"]},
              "public": True, "public_tools": view.get("public_tools", []), "rev": view["rev"]},
    )
    assert put.status_code == 200, put.text


@pytest_asyncio.fixture
async def infra(client, mock_llm, unique_suffix, tmp_path_factory) -> AsyncIterator[Infra]:
    _registry, mock_url = mock_llm
    s = unique_suffix
    made: list[tuple[str, str]] = []  # (plural, id), deleted in reverse order

    async def create(plural: str, body: dict) -> str:
        r = await client.post(f"/v1/{plural}", json=body)
        assert r.status_code == 201, (plural, r.text)
        made.append((plural, body["id"]))
        return body["id"]

    try:
        llm = f"mx-llm-{s}"
        seeded = await seed_llm_provider(client, {
            "id": llm, "provider": "openchat", "models": [{"name": "scripted:default", "context_length": 8192}],
            "config": {"url": mock_url, "flavor": "lmstudio"}, "limits": {"max_concurrency": 1},
        })
        assert seeded.status_code in (200, 201), seeded.text
        made.append(("llm_providers", llm))
        profile = profile_id_for(llm, "scripted:default")
        made.append(("model_profiles", profile))
        wsp = await create("workspace_providers", {
            "id": f"mx-wsp-{s}", "provider": "local", "config": {"kind": "local", "root_path": str(tmp_path_factory.mktemp(f"mx-ws-{s}"))},
        })
        tpl = await create("workspace_templates", {"id": f"mx-base-tpl-{s}", "description": "matrix base", "provider_id": wsp, "backend": {"kind": "local"}})
        chp = await create("channel_providers", {
            "id": f"mx-chp-{s}", "provider": "slack",
            "config": {"app_token": "xapp-1-matrix", "bot_token": "xoxb-1-matrix", "signing_secret": "matrix-secret"},
        })
        emb = await create("embedding_providers", {
            "id": f"mx-emb-{s}", "provider": "openai", "models": [{"name": "stub-embed"}],
            "config": {"url": "http://127.0.0.1:1", "api_key": "sk-not-used", "flavor": "other"}, "limits": {"max_concurrency": 1},
        })
        ce = await create("cross_encoder_providers", {
            "id": f"mx-ce-{s}", "provider": "huggingface", "models": [{"name": "BAAI/bge-reranker-v2-m3"}],
            "config": {"token": None}, "limits": {"max_concurrency": 1},
        })
        ssp = await create("ssp", pgvector_ssp_body(f"mx-ssp-{s}"))
        for plural, rid in (("llm_providers", llm), ("model_profiles", profile), ("workspace_providers", wsp),
                            ("workspace_templates", tpl), ("channel_providers", chp)):
            await _publish(client, plural, rid)
        yield Infra(llm, profile, wsp, tpl, chp, emb, ce, ssp)
    finally:
        for plural, rid in reversed(made):
            await client.delete(f"/v1/{plural}/{rid}")


# ---- the kinds -------------------------------------------------------------------------------------------------------------------------------


@dataclass
class Kind:
    """One owned kind: how A creates a row, and the REST route and MCP tools every check goes through."""

    name: str
    plural: str
    create: Callable[..., Any]  # async (client, suffix, ctx) -> row id (None: the server cannot make one)
    tools: tuple[str, str, str]  # get, update, delete (built-in MCP tool ids)
    writable: bool = True  # False: writes are admin-only (a harness): only the read levels are checked
    mcp_update: Callable[[str], dict] = lambda rid: {"id": rid, "entity": {"id": rid, "description": "edited"}}  # noqa: E731
    rest_update: Callable[[dict], dict] = lambda row: {**row, "description": "edited"}  # noqa: E731
    rest_method: str = "put"  # the REST verb that edits the row (a workspace is only renamed: PATCH)
    mcp_update_tool: bool = True  # False: no MCP tool edits this row (a workspace row is edited over REST only)
    mcp_writes: bool = True  # False: the writes are approval-gated by the seeded policies, so MCP does not expose them ("not exposed: approval_required")


async def _profile(client: httpx.AsyncClient, ctx: dict) -> str:
    """The published profile of ``infra``, checked to be one this user may use."""
    items = (await client.get("/v1/model_profiles/choices", params={"limit": 200})).json().get("items", [])
    assert ctx["infra"].profile in [i["id"] for i in items], items
    return ctx["infra"].profile


async def _make_agent(c, suffix, ctx):
    rid = f"mx-agent-{suffix}"
    made = await c.post("/v1/agents", json={"id": rid, "description": "matrix", "model": {"profile_id": await _profile(c, ctx)}})
    assert made.status_code == 201, made.text
    return rid


async def _make_graph(c, suffix, ctx):
    rid = f"mx-graph-{suffix}"
    made = await c.post("/v1/graphs", json={"id": rid, "description": "matrix", "nodes": [], "edges": []})
    assert made.status_code == 201, made.text
    return rid


async def _make_toolset(c, suffix, ctx):
    rid = f"mx-toolset-{suffix}"
    made = await c.post("/v1/toolsets", json={"id": rid, "provider": "internal"})
    assert made.status_code == 201, made.text
    return rid


async def _make_collection(c, suffix, ctx):
    rid = f"mx-kb-{suffix}"
    made = await c.post("/v1/collections", json={"id": rid, "description": "matrix"})
    assert made.status_code == 201, made.text
    return rid


async def _make_template(c, suffix, ctx):
    infra = ctx["infra"]
    rid = f"mx-tpl-{suffix}"
    made = await c.post(
        "/v1/workspace_templates",
        json={"id": rid, "description": "matrix", "provider_id": infra.ws_provider, "backend": {"kind": "local"}},
    )
    assert made.status_code == 201, made.text
    return rid


async def _make_workspace(c, suffix, ctx):
    made = await c.post("/v1/workspaces", json={"template_id": ctx["infra"].template})
    assert made.status_code == 201, made.text
    return made.json()["id"]


async def _make_trigger(c, suffix, ctx):
    fire_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    made = await c.post("/v1/triggers", json={"slug": f"mx-trg-{suffix}", "name": "matrix", "config": {"kind": "delayed", "fire_at": fire_at}})
    assert made.status_code == 201, made.text
    return made.json()["id"]


async def _make_channel(c, suffix, ctx):
    rid = f"mx-ch-{suffix}"
    made = await c.post("/v1/channels", json={"id": rid, "provider_id": ctx["infra"].channel_provider, "provider": "slack", "external_id": f"MX{suffix}"[:30]})
    assert made.status_code == 201, made.text
    return rid


async def _make_harness(c, suffix, ctx):
    # Harness writes are admin-only: the admin creates it, and the owner column is the admin.
    made = await c.post("/v1/harnesses", json={"slug": f"mx-hn-{suffix}", "name": "matrix", "git_url": "https://github.com/example/matrix.git"})
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


KINDS = [
    Kind("agent", "agents", _make_agent, ("system__get_agent", "system__update_agent", "system__delete_agent"), mcp_writes=False),
    Kind("graph", "graphs", _make_graph, ("system__get_graph", "system__update_graph", "system__delete_graph"), mcp_writes=False),
    Kind("toolset", "toolsets", _make_toolset, ("system__get_toolset", "system__update_toolset", "system__delete_toolset")),
    Kind("collection", "collections", _make_collection, ("system__get_collection", "system__update_collection", "system__delete_collection")),
    Kind("workspace_template", "workspace_templates", _make_template,
         ("workspaces__get_workspace_template", "workspaces__update_workspace_template", "workspaces__delete_workspace_template")),
    Kind("workspace", "workspaces", _make_workspace, ("workspaces__get_workspace", "workspaces__get_workspace", "workspaces__delete_workspace"),
         mcp_update=lambda rid: {"id": rid}, rest_update=lambda row: {"name": "renamed"}, rest_method="patch", mcp_update_tool=False),
    Kind("trigger", "triggers", _make_trigger, ("trigger__get", "trigger__update", "trigger__delete"),
         mcp_update=lambda rid: {"id": rid, "name": "edited"}, rest_update=lambda row: {"name": "edited"}, mcp_writes=False),
    Kind("channel", "channels", _make_channel, ("system__get_channel", "system__update_channel", "system__delete_channel"),
         rest_update=lambda row: row),
    Kind("harness", "harnesses", _make_harness, ("harness__get", "harness__get", "harness__get"), writable=False),
]


# ---- the two doors ---------------------------------------------------------------------------------------------------------------------------


def _outcome(status: int) -> str:
    return OK if status in (200, 201, 204) else MISSING if status == 404 else DENIED if status == 403 else f"http-{status}"


class RestDoor:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def get(self, kind: Kind, rid: str) -> str:
        return _outcome((await self.client.get(f"/v1/{kind.plural}/{rid}")).status_code)

    async def update(self, kind: Kind, rid: str) -> str:
        current = await self.client.get(f"/v1/{kind.plural}/{rid}")
        if current.status_code != 200:
            return _outcome(current.status_code)
        send = getattr(self.client, kind.rest_method)
        return _outcome((await send(f"/v1/{kind.plural}/{rid}", json=kind.rest_update(current.json()))).status_code)

    async def delete(self, kind: Kind, rid: str) -> str:
        return _outcome((await self.client.delete(f"/v1/{kind.plural}/{rid}")).status_code)

    async def listed(self, kind: Kind, rid: str) -> bool:
        items = (await self.client.get(f"/v1/{kind.plural}", params={"limit": 200})).json().get("items", [])
        return rid in [i["id"] for i in items]


class McpDoor:
    """The same checks through an MCP client: the matching built-in tool, called with the person's own bearer token."""

    def __init__(self, session) -> None:
        self.session = session

    async def _call(self, tool: str, arguments: dict) -> tuple[str, str]:
        try:
            result = await self.session.call_tool(
                "call_tool",
                arguments={"scoped_id": tool, "arguments": arguments},
            )
        except Exception as exc:  # noqa: BLE001 - the SDK raises the JSON-RPC error for a tool that is not exposed
            return (MISSING if "not exposed" in str(exc).lower() or "method" in str(exc).lower() else f"error-{exc!r}"), ""
        text = " ".join(getattr(part, "text", "") for part in result.content)
        try:
            envelope = json.loads(text)
        except ValueError:
            return f"meta-error-{text}", text
        if envelope.get("ok") is True:
            return OK, str(envelope.get("output", ""))
        # The M2 refusal is the fixed nested envelope {"ok": false, "error": {"code", "message"}} (isError true): it
        # carries no existence wording on the wire, so a not-exposed or unavailable tool classifies like a missing row.
        error = envelope.get("error") or {}
        code = str(error.get("code", ""))
        if code == "access_denied":
            return DENIED, text
        if code == "tool_unavailable":
            return MISSING, text  # the read level pins the exposure; this answer must not echo existence
        message = str(error.get("message", ""))
        status, verdict = None, ""
        try:  # the tool_failed message is the inner tool's {"type", "message"} error result; RFC7807 shapes carry a numeric status
            inner = json.loads(message)
            if isinstance(inner, dict):
                status = inner.get("status")
                verdict = str(inner.get("type", ""))
        except ValueError:
            pass
        if status == 404:
            return MISSING, text
        if status == 403:
            return DENIED, text
        verdict = verdict or message.lower()
        if "forbidden" in verdict:
            return DENIED, text
        return (MISSING, text) if "not" in verdict and "found" in verdict or "not-found" in verdict else (f"tool-error-{verdict or 'unknown'}", text)

    async def get(self, kind: Kind, rid: str) -> str:
        return (await self._call(kind.tools[0], {"id": rid}))[0]

    async def update(self, kind: Kind, rid: str) -> str:
        outcome, text = await self._call(kind.tools[0], {"id": rid})  # the whole row first, as the REST door does
        if outcome != OK:
            return outcome
        arguments = kind.mcp_update(rid)
        if "entity" in arguments:
            arguments = {"id": rid, "entity": kind.rest_update(json.loads(text))}
        return (await self._call(kind.tools[1], arguments))[0]

    async def delete(self, kind: Kind, rid: str) -> str:
        return (await self._call(kind.tools[2], {"id": rid}))[0]

    async def listed(self, kind: Kind, rid: str) -> bool:  # the MCP door lists through the same get: absent rows are not found
        return await self.get(kind, rid) == OK


@asynccontextmanager
async def _mcp_door(admin: httpx.AsyncClient, user: httpx.AsyncClient, base_url: str, tools: list[str], label: str) -> AsyncIterator[McpDoor]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    catalogue = {i["id"] for i in (await admin.get("/v1/tools/catalogue")).json().get("items", [])}
    exposable = sorted(set(tools) & catalogue)
    # mode "allowlist" makes the saved allowlist the gate: a fresh exposure row defaults to "rbac" (caller-aware),
    # which would ignore allowed_tools. The journey's exposure proof needs the legacy narrowing.
    enabled = await admin.put("/v1/mcp_exposure", json={"enabled": True, "mode": "allowlist", "allowed_tools": exposable})
    assert enabled.status_code in (200, 204), enabled.text
    token = await user.post("/v1/auth/tokens", json={"name": label, "scopes": ["mcp"]})
    assert token.status_code == 201, token.text
    headers = {"Authorization": f"Bearer {token.json()['plaintext']}"}
    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(f"{base_url}/v1/mcp/", http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield McpDoor(session)


# ---- 1. every owned kind, every level, both doors ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
async def test_every_level_of_every_owned_kind_through_rest_and_mcp(client, base_url, unique_suffix, infra, kind: Kind) -> None:
    all_tools = sorted({t for k in KINDS for t in k.tools})
    ctx: dict = {"infra": infra}
    maker = client if kind.name == "harness" else None  # a harness is made by the admin; the others by A
    async with (
        _user(client, base_url, f"mx-a-{unique_suffix}") as (a_id, a),
        _user(client, base_url, f"mx-b-{unique_suffix}") as (b_id, b),
        _user(client, base_url, f"mx-c-{unique_suffix}", role="restricted") as (c_id, c),
    ):
        rid = await kind.create(maker or a, unique_suffix, ctx)
        owner_client = maker or a
        owner_id = (await client.get("/v1/auth/status")).json()["principal"]["user_id"] if maker else a_id
        try:
            async with _mcp_door(client, b, base_url, all_tools, f"mx-b-{kind.name}") as b_mcp:
                for door_name, door in (("rest", RestDoor(b)), ("mcp", b_mcp)):
                    where = f"{kind.name} via {door_name}"
                    # none: absent from the list and answered like a missing row
                    await _share(owner_client, kind.plural, rid, owner_id, {})
                    assert await door.get(kind, rid) == MISSING, where
                    assert not await door.listed(kind, rid), where
                    # read: get ok, update refused
                    await _share(owner_client, kind.plural, rid, owner_id, {b_id: "read"})
                    assert await door.get(kind, rid) == OK, where
                    # Over MCP an approval-gated write (agent, graph, trigger) is not exposed at all: the REST door pins those levels.
                    on_door = door_name == "rest" or kind.mcp_writes
                    has_update = kind.writable and on_door and (door_name == "rest" or kind.mcp_update_tool)
                    if has_update:
                        assert await door.update(kind, rid) == DENIED, where
                    # full: update ok, delete refused
                    if kind.writable:
                        await _share(owner_client, kind.plural, rid, owner_id, {b_id: "full"})
                        if has_update:
                            assert await door.update(kind, rid) == OK, where
                        if on_door:
                            assert await door.delete(kind, rid) == DENIED, where

                # public: restricted C reads nothing; promoted, C reads it
                await _share(owner_client, kind.plural, rid, owner_id, {}, public=True)
                # (a restricted account is turned away by the role gate, 403 on most routes and 404 on the rest: either way it reads nothing)
                assert await RestDoor(c).get(kind, rid) in (MISSING, DENIED), kind.name
                await _set_role(client, c_id, "user")
                assert await RestDoor(c).get(kind, rid) == OK, kind.name
                await _set_role(client, c_id, "restricted")
                assert await RestDoor(c).get(kind, rid) in (MISSING, DENIED), kind.name

                # owner transfer: B becomes the owner and deletes it
                if kind.writable:
                    await _share(owner_client, kind.plural, rid, owner_id, {}, owners=[b_id])
                    assert await RestDoor(b).delete(kind, rid) == OK, kind.name
        finally:
            await client.delete(f"/v1/{kind.plural}/{rid}")


# ---- 1b. grantable infrastructure: use grants, admin-only edits -------------------------------------------------------------------------------

#: (REST plural, the ``kind`` of ``system__get_entity_access`` / ``system__set_entity_access``, the ``Infra`` field holding the row the admin made).
#: The semantic search provider's REST plural is ``ssp``.
GRANTABLE = [
    ("model_profiles", "model_profile", "profile"),
    ("llm_providers", "llm_provider", "llm_provider"),
    ("embedding_providers", "embedding_provider", "embedding"),
    ("cross_encoder_providers", "cross_encoder_provider", "cross_encoder"),
    ("ssp", "semantic_search_provider", "ssp"),
    ("workspace_providers", "workspace_provider", "ws_provider"),
    ("channel_providers", "channel_provider", "channel_provider"),
]


@pytest.mark.parametrize(("plural", "kind", "field"), GRANTABLE, ids=[k for _p, k, _f in GRANTABLE])
async def test_a_use_grant_is_set_by_the_admin_alone_and_read_by_its_grantee(
    client, base_url, unique_suffix, infra, plural: str, kind: str, field: str,
) -> None:
    rid = getattr(infra, field)
    before = (await client.get(f"/v1/{plural}/{rid}/access")).json()
    async with (
        _user(client, base_url, f"mx-g-{unique_suffix}") as (g_id, grantee),
        _user(client, base_url, f"mx-n-{unique_suffix}") as (_n_id, nobody),
        _user(client, base_url, f"mx-r-{unique_suffix}", role="restricted") as (_r_id, restricted),
    ):
        try:
            # none: the first row of a bootstrapped server may be public (the seeded ones are), which every full user reads; make it private first.
            # Neither a plain user nor a restricted one then sees the access view; admin and non-admin writes are 404/403
            await _share(client, plural, rid, "", {}, owners=[], public=False)
            for who in (nobody, restricted):
                assert (await who.get(f"/v1/{plural}/{rid}/access")).status_code in (403, 404), kind
            await _share(client, plural, rid, "", {g_id: "read"}, owners=[], public=False)
            # read (= use): the grantee reads the view of their own level, and cannot change it
            view = await grantee.get(f"/v1/{plural}/{rid}/access")
            assert view.status_code == 200 and view.json()["your_level"] == "read", (kind, view.text)
            assert view.json()["owners"] == [] and view.json()["grants"] == [], "a use grant does not list its peers"
            assert (await grantee.put(f"/v1/{plural}/{rid}/access", json={"owners": [], "grants": {}, "rev": 0})).status_code in (403, 404), kind
            # owner: only an admin edits; full is not a use grant
            admin_rev = (await client.get(f"/v1/{plural}/{rid}/access")).json()["rev"]  # the grantee's view hides the rev: the admin's is current
            refused = await client.put(f"/v1/{plural}/{rid}/access", json={"owners": [g_id], "grants": {}, "rev": admin_rev})
            assert refused.status_code in (400, 422), (kind, refused.text)
            # the nobody and the restricted user still cannot see it
            for who in (nobody, restricted):
                assert (await who.get(f"/v1/{plural}/{rid}/access")).status_code in (403, 404), kind
        finally:
            await client.put(
                f"/v1/{plural}/{rid}/access",
                json={"owners": [], "grants": {g["user"]["id"]: g["level"] for g in before["grants"]}, "public": before["public"],
                      "public_tools": [], "rev": (await client.get(f"/v1/{plural}/{rid}/access")).json()["rev"]},
            )


# ---- 2. inherited rows ------------------------------------------------------------------------------------------------------------------------


async def test_inherited_rows_follow_their_parent(client, base_url, unique_suffix) -> None:
    cid = f"mx-inh-kb-{unique_suffix}"
    async with (
        _user(client, base_url, f"mx-a2-{unique_suffix}") as (a_id, a),
        _user(client, base_url, f"mx-b2-{unique_suffix}") as (b_id, b),
    ):
        assert (await a.post("/v1/collections", json={"id": cid, "description": "inherit"})).status_code == 201
        trigger_id = await _make_trigger(a, unique_suffix, {})
        try:
            doc = await a.post(f"/v1/collections/{cid}/docs", json={"parent": "", "slug": "intro", "body": "inherited body"})
            assert doc.status_code == 201, doc.text
            doc_id = doc.json()["document"]["id"]
            sub_body = {"config": {"kind": "agent_fresh_session", "workspace_id": "none-needed", "agent_id": "none-needed"}}

            assert (await b.get(f"/v1/documents/{doc_id}")).status_code == 404
            await _share(a, "collections", cid, a_id, {b_id: "read"})
            assert (await b.get(f"/v1/documents/{doc_id}")).status_code == 200
            assert (await b.post(f"/v1/collections/{cid}/docs", json={"parent": "", "slug": "b-note", "body": "x"})).status_code == 403
            await _share(a, "collections", cid, a_id, {b_id: "full"})
            assert (await b.post(f"/v1/collections/{cid}/docs", json={"parent": "", "slug": "b-note", "body": "x"})).status_code == 201

            assert (await b.get(f"/v1/triggers/{trigger_id}/subscriptions")).status_code == 404
            await _share(a, "triggers", trigger_id, a_id, {b_id: "read"})
            assert (await b.get(f"/v1/triggers/{trigger_id}/subscriptions")).status_code == 200
            assert (await b.post(f"/v1/triggers/{trigger_id}/subscriptions", json=sub_body)).status_code == 403
        finally:
            await a.delete(f"/v1/collections/{cid}")
            await a.delete(f"/v1/triggers/{trigger_id}")


# ---- 3. infrastructure ------------------------------------------------------------------------------------------------------------------------


async def test_an_agent_on_a_private_profile_is_refused_until_the_admin_grants_use(client, base_url, unique_suffix, infra) -> None:
    private = infra.profile
    async with _user(client, base_url, f"mx-b3-{unique_suffix}") as (b_id, b):
        await _share(client, "model_profiles", private, "", {}, owners=[], public=False)
        refused = await b.post("/v1/agents", json={"id": f"mx-prof-{unique_suffix}", "description": "x", "model": {"profile_id": private}})
        assert refused.status_code in (404, 422), refused.text
        await _share(client, "model_profiles", private, "", {b_id: "read"}, owners=[])
        made = await b.post("/v1/agents", json={"id": f"mx-prof-{unique_suffix}", "description": "x", "model": {"profile_id": private}})
        try:
            assert made.status_code == 201, made.text
        finally:
            await b.delete(f"/v1/agents/mx-prof-{unique_suffix}")
            await _share(client, "model_profiles", private, "", {}, owners=[], public=True)


# ---- 5. events --------------------------------------------------------------------------------------------------------------------------------


async def test_events_are_attributed_and_scoped(client, base_url, unique_suffix, infra) -> None:
    async with (
        _user(client, base_url, f"mx-a5-{unique_suffix}") as (a_id, a),
        _user(client, base_url, f"mx-b5-{unique_suffix}") as (_b_id, b),
    ):
        wid = await _make_workspace(a, unique_suffix, {"infra": infra})
        rid = await _make_agent(a, unique_suffix, {"infra": infra})
        try:
            events = (await client.get("/v1/events", params={"event_type": "agent.created", "limit": 200})).json().get("items", [])
            mine = [e for e in events if e.get("entity_id") == rid]
            assert mine and mine[0]["actor"] == f"user:{a_id}", mine
            scoped = await b.get("/v1/events", params={"workspace_id": wid})
            assert scoped.status_code == 404, scoped.text
        finally:
            await a.delete(f"/v1/agents/{rid}")
            await a.delete(f"/v1/workspaces/{wid}")


# ---- 6. lifecycle -----------------------------------------------------------------------------------------------------------------------------


async def test_deleting_an_owner_hands_over_and_demotion_hides_until_promotion(client, base_url, unique_suffix) -> None:
    cid = f"mx-life-kb-{unique_suffix}"
    admin_id = (await client.get("/v1/auth/status")).json()["principal"]["user_id"]
    made_a = await client.post("/v1/admin/users", json={"username": f"mx-a6-{unique_suffix}", "password": _PASSWORD, "role": "user"})
    assert made_a.status_code == 201, made_a.text
    a_id = made_a.json()["id"]
    async with _user(client, base_url, f"mx-b6-{unique_suffix}") as (b_id, b):
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0)) as a:
            assert (await a.post("/v1/auth/login", json={"username": f"mx-a6-{unique_suffix}", "password": _PASSWORD})).status_code == 200
            assert (await a.post("/v1/collections", json={"id": cid, "description": "life"})).status_code == 201
            await _share(a, "collections", cid, a_id, {b_id: "read"})
            try:
                # The admin deletes A: the sole-owned row is the admin's, B's grant survives.
                assert (await client.delete(f"/v1/admin/users/{a_id}")).status_code == 204
                view = (await client.get(f"/v1/collections/{cid}/access")).json()
                assert [o["id"] for o in view["owners"]] == [admin_id], view
                assert {g["user"]["id"]: g["level"] for g in view["grants"]} == {b_id: "read"}, view
                assert (await b.get(f"/v1/collections/{cid}")).status_code == 200

                # The admin demotes B: B reads nothing; promotion restores it.
                await _set_role(client, b_id, "restricted")
                assert (await b.get(f"/v1/collections/{cid}")).status_code in (403, 404)
                await _set_role(client, b_id, "user")
                assert (await b.get(f"/v1/collections/{cid}")).status_code == 200
            finally:
                await client.delete(f"/v1/collections/{cid}")
