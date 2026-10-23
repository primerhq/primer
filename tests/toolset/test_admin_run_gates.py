"""A run started by an ADMIN meets the approval gates too (ticket 01a16a29, the admin half of audit G6-1).

#851 gated every door to the agent, graph and trigger writes, but three groups of writes had no gate at all, so a run at an admin's rank (the seeded
operator's main session usually runs at its creator's) walked around the approval system:

* the tool approval policy writes (``system`` create / update / delete_tool_approval_policy): a model could delete or relax the very gate that
  protects it, then make the write;
* ``system::create_toolset`` / ``update_toolset`` whose body is a python toolset (its source runs on the server host) or a stdio MCP toolset (it
  launches a command there), while ``crud::create_python_toolset`` parks: the G6-1 twin gap again;
* ``harness::harness__install`` / ``harness__sync`` / ``harness__uninstall``: they create, update and delete a bundle's agents, graphs, triggers
  and toolsets.

The table below is every such tool (the completeness test reads the live listings of every built-in toolset). Each is called by an admin-role run
through every door a model has to it, on SQLite (whose ``find`` honours the predicate, so a lookup under the wrong key cannot pass): a system tool
directly through the tool manager and through ``system__call_tool``, each also under the retired ``_system`` spelling; a harness tool through
``system__call_tool`` only (its bare ids carry ``__``, so the tool manager refuses to mount the harness toolset, and call_tool is its one door in a
run). Two worlds: a FRESH install (the ensure pass seeds every gate) and an UPGRADED one (it holds only the 18 policies #851 seeded; the boot runs
the migrations, then the ensure pass). Every call must park on the seeded policy of its own key before anything is written. Over MCP, which has no
park, every one of them is refused as ``approval_required``.

The toolset writes are gated on the BODY: an http or sse MCP toolset still runs ungated, a body that turns an http toolset into a stdio or python
one parks, and no spelling of a python or stdio body the handler accepts gets past the shipped Rego gate.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from primer.agent.approval import ApprovalContext, ApprovalResolver, evaluate_approval_gate
from primer.agent.tool_manager import ToolExecutionManager
from primer.api.registries import ProviderRegistry
from primer.bootstrap.seed import crud_policy_id, default_policy_id, ensure_crud_approval_policies
from primer.model.chat import ToolCallPart
from primer.model.harness import Harness, HarnessStatus
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.providers.toolset import Toolset
from primer.model.storage import OffsetPage
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.yield_ import YieldToWorker
from primer.session.approvers import ADMIN_ONLY_SPEC
from primer.storage.migrations import LATEST_VERSION, run_migrations
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.crud import CRUD_TOOL_NAMES, CRUD_TWINS, DEFAULT_GATED_TOOLS, build_crud_toolset
from primer.toolset.harness import build_harness_toolset_provider
from primer.toolset.system import build_system_toolset
from primer.toolset.toolset_checks import launches_a_command, runs_python
from primer.toolset.trigger import build_trigger_toolset_provider

ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")

PY_SOURCE = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person.\n\n    Use when greeting.\n\n'
    '    Args:\n        name: Who.\n    """\n'
    "    return 'hi ' + name\n"
)
PYTHON = {"id": "ts-py", "provider": "python", "config": {"source": PY_SOURCE, "source_version": 1}}
STDIO = {"id": "ts-stdio", "provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["/bin/sh", "-c", "id"]}}}
HTTP = {"id": "ts-http", "provider": "mcp", "config": {"transport": "http", "config": {"url": "https://mcp.example.com/mcp"}}}
INTERNAL = {"id": "ts-int", "provider": "internal"}
SSE = {"id": "ts-sse", "provider": "mcp", "config": {"transport": "sse", "config": {"url": "https://mcp.example.com/sse"}}}
POLICY = {"id": "mine", "toolset_id": "misc", "tool_name": "get_datetime", "enabled": True, "approval": {"type": "required"}}
RELAXED = {"id": crud_policy_id("create_agent"), "toolset_id": "crud", "tool_name": "create_agent", "enabled": False, "approval": {"type": "required"}}

#: ``(case, toolset_id, tool_name, arguments, approval type of the shipped gate)``: every write an admin-role run makes that must pause.
GATED: list[tuple[str, str, str, dict[str, Any], str]] = [
    ("create-a-policy", "system", "create_tool_approval_policy", {"entity": POLICY}, "required"),
    ("relax-the-gate-on-create-agent", "system", "update_tool_approval_policy", {"id": RELAXED["id"], "entity": RELAXED}, "required"),
    ("delete-the-gate-on-create-agent", "system", "delete_tool_approval_policy", {"id": crud_policy_id("create_agent")}, "required"),
    (
        "delete-the-gate-on-deleting-gates", "system", "delete_tool_approval_policy",
        {"id": default_policy_id("system", "delete_tool_approval_policy")}, "required",
    ),
    ("create-a-python-toolset", "system", "create_toolset", {"entity": PYTHON}, "policy"),
    ("create-a-stdio-toolset", "system", "create_toolset", {"entity": STDIO}, "policy"),
    ("turn-http-into-stdio", "system", "update_toolset", {"id": "ts-http", "entity": {**STDIO, "id": "ts-http"}}, "policy"),
    ("turn-http-into-python", "system", "update_toolset", {"id": "ts-http", "entity": {**PYTHON, "id": "ts-http"}}, "policy"),
    ("install-a-harness", "harness", "harness__install", {"id": "hns-draft", "overrides": {}}, "required"),
    ("sync-a-harness", "harness", "harness__sync", {"id": "hns-installed", "overrides": {}}, "required"),
    ("uninstall-a-harness", "harness", "harness__uninstall", {"id": "hns-installed"}, "required"),
]

#: ``(case, tool_name, arguments)``: toolset writes whose body runs no code on the server host; an admin-role run makes them ungated.
UNGATED_TOOLSET_WRITES: list[tuple[str, str, dict[str, Any]]] = [
    ("create-an-http-toolset", "create_toolset", {"entity": {**HTTP, "id": "ts-http-2"}}),
    ("create-an-sse-toolset", "create_toolset", {"entity": SSE}),
    ("move-an-http-toolset", "update_toolset", {"id": "ts-http", "entity": {**HTTP, "config": {"transport": "http", "config": {"url": "https://b.example.com/mcp"}}}}),
]

# The doors a model has to one of these tools in a run: a system tool directly and through ``system__call_tool``, both again under the retired
# ``_system`` spelling the registry still resolves to the same provider; a harness tool through ``system__call_tool`` only.
_DOORS = {
    "system": ["direct", "through-call-tool", "direct-as-_system", "through-call-tool-as-_system"],
    "harness": ["through-call-tool"],
}

# The tools the table must cover, by name, in every built-in toolset: a policy write, a toolset write, a harness lifecycle operation.
_GATED_NAME = re.compile(r"^((create|update|delete)_tool_approval_polic(y|ies)|(create|update)_toolset|harness__(install|sync|uninstall))$")

# What #851 seeded, the whole gate set of an install at schema version 15, the last before these gates.
_GATES_OF_851 = (*(("crud", name) for name in CRUD_TOOL_NAMES), *CRUD_TWINS)


def _page(length: int = 200) -> OffsetPage:
    return OffsetPage(offset=0, length=length)


class _Session:
    session_id = "sess-1"
    workspace_id = "ws-1"
    agent_id = "operator"
    workspace_tools: list = []


async def _world(tmp_path: Path, kind: str):
    """The storage, registry, toolsets and resolver of an install that is ``fresh`` or ``upgraded`` (see the module docstring), with the rows the
    table's calls would change: an http toolset and two harnesses an install and a sync accept."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / f"{kind}.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    if kind == "fresh":
        await ensure_crud_approval_policies(sp)
    else:
        store = sp.get_storage(ToolApprovalPolicy)
        for toolset_id, tool_name in _GATES_OF_851:
            await store.create(
                ToolApprovalPolicy(id=default_policy_id(toolset_id, tool_name), toolset_id=toolset_id, tool_name=tool_name, approval=RequiredApprovalConfig()),
            )
        await sp.set_bootstrap_completed(datetime.now(UTC))
        await sp.set_schema_version(15)
        assert await run_migrations(sp, is_fresh_install=False) == LATEST_VERSION
        await ensure_crud_approval_policies(sp)
    await sp.get_storage(Toolset).create(Toolset.model_validate(HTTP))
    now = datetime.now(UTC)
    harnesses = sp.get_storage(Harness)
    await harnesses.create(Harness(id="hns-draft", slug="draft", name="Draft", overrides_schema={"type": "object"}, created_at=now))
    await harnesses.create(
        Harness(id="hns-installed", slug="installed", name="Installed", status=HarnessStatus.INSTALLED, available_bundle_hash="b1", created_at=now),
    )
    registry = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
    )
    resolver = ApprovalResolver(storage=sp.get_storage(ToolApprovalPolicy))
    system = build_system_toolset(storage_provider=sp, provider_registry=registry, approval_resolver=resolver)
    harness = build_harness_toolset_provider(storage_provider=sp)
    crud = build_crud_toolset(storage_provider=sp)
    trigger = build_trigger_toolset_provider(storage_provider=sp)
    registry._system_toolset_provider = system  # type: ignore[attr-defined]
    registry._harness_toolset_provider = harness  # type: ignore[attr-defined]
    registry._crud_toolset_provider = crud  # type: ignore[attr-defined]
    registry._trigger_toolset_provider = trigger  # type: ignore[attr-defined]
    # The harness toolset is the registry's only: the tool manager cannot mount it (see the module docstring).
    toolsets = {"system": system, "_system": system, "crud": crud, "trigger": trigger}
    return sp, registry, toolsets, resolver


def _manager(toolsets, resolver, registry) -> ToolExecutionManager:
    system = [n for _, t, n, _, _ in GATED if t == "system"]
    tools = ["system__call_tool", *(f"system__{n}" for n in system), *(f"_system__{n}" for n in system)]
    tools += [f"system__{n}" for _, n, _ in UNGATED_TOOLSET_WRITES]
    return ToolExecutionManager.for_workspace(
        toolset_providers=toolsets, session=_Session(), approval_resolver=resolver, provider_registry=registry, tools=tools, initiated_by=ADMIN,
    )


def _call(door: str, toolset_id: str, tool_name: str, arguments: dict[str, Any]) -> ToolCallPart:
    spelled = "_system" if door.endswith("as-_system") else toolset_id
    if door.startswith("direct"):
        return ToolCallPart(id="c1", name=f"{spelled}__{tool_name}", arguments=arguments)
    return ToolCallPart(id="c1", name="system__call_tool", arguments={"toolset_id": spelled, "tool_name": tool_name, "arguments": arguments})


async def _state(sp) -> dict[str, list[dict[str, Any]]]:
    """Every row a table call could write: the policies, the toolsets and the harnesses, as stored."""
    out = {}
    for model in (ToolApprovalPolicy, Toolset, Harness):
        rows = (await sp.get_storage(model).list(_page())).items
        out[model.__name__] = sorted((row.model_dump(mode="json") for row in rows), key=lambda r: r["id"])
    return out


def _cases():
    for case, toolset_id, tool_name, arguments, gate in GATED:
        for door in _DOORS[toolset_id]:
            yield pytest.param(toolset_id, tool_name, arguments, gate, door, id=f"{case}-{door}")


@pytest.mark.parametrize("toolset_id, tool_name, arguments, gate, door", list(_cases()))
@pytest.mark.parametrize("kind", ["fresh", "upgraded"])
async def test_an_admin_run_parks_on_the_seeded_gate_before_anything_is_written(tmp_path, kind, toolset_id, tool_name, arguments, gate, door):
    sp, registry, toolsets, resolver = await _world(tmp_path, kind)
    try:
        before = await _state(sp)

        with pytest.raises(YieldToWorker) as parked:
            await _manager(toolsets, resolver, registry).execute(_call(door, toolset_id, tool_name, arguments))

        meta = parked.value.yielded.resume_metadata
        assert parked.value.yielded.tool_name == "_approval"
        assert meta["policy_id"] == default_policy_id(toolset_id, tool_name), meta
        assert meta["approval_type"] == gate
        assert meta["approvers"] == ADMIN_ONLY_SPEC, "an admin-tier gate is decided by an admin only"
        if "call-tool" in door:
            assert meta["via_call_tool"] == {"toolset_id": toolset_id, "tool_name": tool_name}
        assert await _state(sp) == before, "a row changed before the gate was decided"
    finally:
        await sp.aclose()


@pytest.mark.parametrize("toolset_id, tool_name, arguments", [(t, n, a) for _, t, n, a, _ in GATED], ids=[c for c, *_ in GATED])
async def test_only_an_admin_may_decide_an_admin_tier_gate(tmp_path, toolset_id, tool_name, arguments):
    """The lead's ruling on #860 (R860-6): the shipped admin-tier gates carry the admin-only approver spec, so a user cannot approve one over
    REST and a chat-platform user primer cannot identify cannot answer one from a channel; an admin can."""
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException

    from primer.api.approver_guard import enforce_approvers
    from primer.bus.in_memory import InMemoryEventBus
    from primer.channel.adapter import ResponseEnvelope
    from primer.channel.inbox import ChannelInbox
    from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
    from primer.session.approvers import ApproverRefusedError, may_decide
    from tests.conftest import _FakeStorageProvider

    sp, registry, toolsets, resolver = await _world(tmp_path, "fresh")
    try:
        with pytest.raises(YieldToWorker) as parked:
            await _manager(toolsets, resolver, registry).execute(_call("through-call-tool", toolset_id, tool_name, arguments))
    finally:
        await sp.aclose()
    meta = parked.value.yielded.resume_metadata

    assert may_decide(meta, username="root", role="admin")
    assert not may_decide(meta, username="bob", role="user")
    assert not may_decide(meta, username=None, role=None), "an unidentified decider answered an admin-only gate"
    enforce_approvers(meta, SimpleNamespace(username="root", role="admin"))
    with pytest.raises(HTTPException) as refused:
        enforce_approvers(meta, SimpleNamespace(username="bob", role="user"))
    assert refused.value.status_code == 403

    # A Slack user answers the parked gate from a channel: refused before anything reaches the bus.
    now = datetime.now(UTC)
    event_key = "tool_approval:s-1:c1"
    session = WorkspaceSession(
        id="s-1", workspace_id="ws-1", binding=AgentSessionBinding(kind="agent", agent_id="operator"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=event_key,
        parked_state={"tool_call_id": "c1", "yielded": {"tool_name": "_approval", "event_key": event_key, "resume_metadata": meta}},
    )
    bus = InMemoryEventBus()
    await bus.initialize()
    channel_sp = _FakeStorageProvider()
    await channel_sp.get_storage(WorkspaceSession).create(session)
    events = bus.subscribe()
    try:
        reply = ResponseEnvelope(
            kind="tool_approval", workspace_id="ws-1", session_id="s-1", tool_call_id="c1", response=None, decision="approved", reason=None,
            platform_metadata={"slack_user_id": "U123"},
        )
        with pytest.raises(ApproverRefusedError):
            await ChannelInbox(event_bus=bus, storage_provider=channel_sp).handle_response(reply)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(events), timeout=0.2)
    finally:
        await events.aclose()
        await bus.aclose()


@pytest.mark.parametrize("door", _DOORS["system"])
@pytest.mark.parametrize("tool_name, arguments", [(n, a) for _, n, a in UNGATED_TOOLSET_WRITES], ids=[c for c, _, _ in UNGATED_TOOLSET_WRITES])
async def test_a_toolset_write_that_runs_no_code_on_the_host_is_not_gated(tmp_path, tool_name, arguments, door):
    """The gate is on the body: an admin-role run writes an http or sse MCP toolset, or moves one to another URL, without an approval card."""
    sp, registry, toolsets, resolver = await _world(tmp_path, "fresh")
    try:
        result = await _manager(toolsets, resolver, registry).execute(_call(door, "system", tool_name, arguments))

        assert not result.error, result.output
        stored = await sp.get_storage(Toolset).get(arguments["entity"]["id"])
        assert stored is not None and stored.config.model_dump(mode="json")["config"]["url"] == arguments["entity"]["config"]["config"]["url"]
    finally:
        await sp.aclose()


async def test_the_table_is_every_policy_toolset_and_harness_lifecycle_write_in_every_built_in_toolset(tmp_path):
    """A new tool that writes an approval policy, writes a toolset row from a body, or installs, syncs or uninstalls a harness, in ANY built-in
    toolset, fails here until it is in the table (and so gated)."""
    from tests.docs._live_toolsets import built_in_providers

    found: set[tuple[str, str]] = set()
    for toolset_id, provider in built_in_providers().items():
        async for tool in provider.list_tools():
            if _GATED_NAME.match(tool.id):
                found.add((toolset_id, tool.id))
    assert found == {(t, n) for _, t, n, _, _ in GATED}
    assert found <= set(DEFAULT_GATED_TOOLS)


async def test_both_worlds_end_with_the_same_admin_run_gates(tmp_path):
    """An upgraded install that never touched its policies holds exactly the rows, and the Rego text, a fresh install is seeded with."""
    shapes = {}
    for kind in ("fresh", "upgraded"):
        sp, *_ = await _world(tmp_path, kind)
        try:
            rows = (await sp.get_storage(ToolApprovalPolicy).list(_page())).items
            shapes[kind] = sorted(
                (p.id, p.toolset_id, p.tool_name, p.enabled, repr(p.approval.model_dump(mode="json")), repr(p.approvers and p.approvers.model_dump()))
                for p in rows
            )
        finally:
            await sp.aclose()
    assert shapes["fresh"] == shapes["upgraded"]
    assert {(t, n) for _, t, n, _, _ in GATED} <= {(t, n) for _, t, n, *_ in shapes["fresh"]}


# ---- the toolset gate cannot be walked around by spelling the body differently ---------------------------------------------------------------------


# Bodies a model could send. For each, the handler's own parse (``Toolset.model_validate``) decides whether it runs code on the host; the shipped gate
# must require approval for every body that parses and does, whatever its spelling.
_BODIES: list[tuple[str, Any]] = [
    ("python", PYTHON),
    ("stdio", STDIO),
    ("python-upper", {**PYTHON, "provider": "PYTHON"}),
    ("python-title", {**PYTHON, "provider": "Python"}),
    ("python-padded", {**PYTHON, "provider": " python"}),
    ("mcp-upper-stdio", {**STDIO, "provider": "MCP"}),
    ("stdio-upper", {**STDIO, "config": {"transport": "STDIO", "config": {"command": ["id"]}}}),
    ("stdio-no-transport", {**STDIO, "config": {"config": {"command": ["id"]}}}),
    ("stdio-transport-list", {**STDIO, "config": {"transport": ["stdio"], "config": {"command": ["id"]}}}),
    ("http-transport-stdio-config", {**HTTP, "config": {"transport": "http", "config": {"command": ["id"]}}}),
    ("stdio-transport-http-config", {**HTTP, "config": {"transport": "stdio", "config": {"url": "https://x"}}}),
    ("python-with-type-key", {**HTTP, "type": "python"}),
    ("python-with-an-http-config", {**PYTHON, "config": HTTP["config"]}),
    ("mcp-with-a-python-config", {**HTTP, "config": PYTHON["config"]}),
    ("no-provider", {"id": "x", "config": PYTHON["config"]}),
    ("stdio-with-a-decoy-transport", {**STDIO, "transport": "http"}),
    ("stdio-with-a-decoy-url", {**STDIO, "config": {"transport": "stdio", "config": {"command": ["id"], "url": "https://x"}}}),
    ("python-with-a-decoy-kind", {**PYTHON, "kind": "mcp"}),
    ("stdio-without-an-id", {k: v for k, v in STDIO.items() if k != "id"}),
    ("python-without-an-id", {k: v for k, v in PYTHON.items() if k != "id"}),
    ("not-an-object", '{"provider": "python"}'),
    ("internal", INTERNAL),
    ("http", HTTP),
    ("sse", SSE),
]

# The bodies the gate lets through: exactly an http or sse MCP toolset, or an internal one.
_CLEAN = (HTTP, SSE, INTERNAL)


def _runs_code(body: Any) -> bool | None:
    """What the handler would store: ``True`` for a toolset that runs code on the host, ``False`` for one that does not, ``None`` when it refuses the body."""
    try:
        entity = Toolset.model_validate(body)
    except Exception:  # noqa: BLE001 - the handler answers validation-error and stores nothing
        return None
    return runs_python(entity) or launches_a_command(entity)


def test_the_corpus_holds_several_bodies_the_handler_stores_as_code_on_the_host():
    """The property below is about the bodies that parse and run code; a corpus with none of them would pin nothing."""
    assert len([b for _, b in _BODIES if _runs_code(b)]) >= 7
    assert all(_runs_code(b) is False for b in _CLEAN)


@pytest.mark.parametrize("tool_name", ["create_toolset", "update_toolset"])
@pytest.mark.parametrize("body", [b for _, b in _BODIES], ids=[c for c, _ in _BODIES])
async def test_no_spelling_of_a_body_that_runs_code_gets_past_the_shipped_gate(tmp_path, tool_name, body):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "gate.sqlite"))
    await sp.initialize()
    try:
        await ensure_crud_approval_policies(sp)
        policy = await sp.get_storage(ToolApprovalPolicy).get(default_policy_id("system", tool_name))
        assert policy is not None, "no gate is seeded on the toolset write"
        arguments = {"entity": body} if tool_name == "create_toolset" else {"id": "ts-http", "entity": body}
        context = ApprovalContext(
            tool_name=tool_name, toolset_id="system", arguments=arguments, agent_id=None, session_id=None, chat_id=None, requested_at=datetime.now(UTC),
        )
        verdict = await evaluate_approval_gate(policy=policy, context=context, provider_registry=None)

        if _runs_code(body):
            assert verdict.required, f"{body!r} runs code on the host and was not gated"
        if body in _CLEAN:
            assert _runs_code(body) is False and not verdict.required, f"{body!r} runs no code on the host and was gated"
    finally:
        await sp.aclose()


# ---- over MCP: no park surface, so a gated call is refused --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("toolset_id, tool_name, arguments", [(t, n, a) for _, t, n, a, _ in GATED], ids=[c for c, *_ in GATED])
async def test_over_mcp_every_gated_write_is_refused_as_approval_required(tmp_path, toolset_id, tool_name, arguments):
    from primer.mcp.dispatch import NotExposed, _enforce_approval_gate

    sp, registry, _toolsets, resolver = await _world(tmp_path, "fresh")
    try:
        deps = SimpleNamespace(approval_resolver=resolver, provider_registry=registry)
        with pytest.raises(NotExposed) as refused:
            await _enforce_approval_gate(
                scoped_id=f"{toolset_id}__{tool_name}", toolset_id=toolset_id, bare_name=tool_name, arguments=arguments, principal=None, deps=deps,
            )
        assert refused.value.reason == "approval_required"
        await _enforce_approval_gate(
            scoped_id="system__create_toolset", toolset_id="system", bare_name="create_toolset", arguments={"entity": SSE}, principal=None, deps=deps,
        )
    finally:
        await sp.aclose()


# ---- the approval card -------------------------------------------------------------------------------------------------------------------------------------


async def test_every_gated_tool_declares_a_card_that_names_the_row_and_withholds_code_and_secrets():
    """A card with nothing on it cannot be decided: each gated tool declares what its card may show (design note 01a11cd3-66b0), and never the free
    text that holds code or a pasted secret (a policy's Rego source or judge prompt, a toolset's source, command, env, URL or headers)."""
    from primer.toolset.admin_gates import PREVIEW_ARGS
    from tests.docs._live_toolsets import built_in_providers

    assert set(PREVIEW_ARGS) == {(t, n) for _, t, n, _, _ in GATED}
    for (toolset_id, tool_name), paths in PREVIEW_ARGS.items():
        tool = {t.id: t async for t in built_in_providers()[toolset_id].list_tools()}[tool_name]
        assert tool.preview_args == paths, (toolset_id, tool_name)
        segments = {segment for path in paths for segment in path.split(".")}
        assert not segments & {"source", "command", "env", "url", "headers", "oauth", "policy", "prompt"}, (toolset_id, tool_name, paths)


def test_a_stdio_toolset_card_names_the_toolset_and_its_transport_and_withholds_the_command():
    from primer.api.routers.workspaces import _approval_preview
    from primer.toolset.admin_gates import PREVIEW_ARGS

    secret = "Zq7!pLm2$vXr9&kTe4"
    body = {**STDIO, "config": {"transport": "stdio", "config": {"command": ["/bin/sh", "-c", f"curl -H 'k: {secret}' x"], "env": {"K": secret}}}}
    card = _approval_preview(
        {"name": "system__create_toolset", "arguments": {"entity": body}}, {"paths": list(PREVIEW_ARGS[("system", "create_toolset")]), "source": "tool"},
    )

    assert secret not in card["arguments"] + " ".join(card["hidden_keys"])
    assert '"provider": "mcp"' in card["arguments"] and '"transport": "stdio"' in card["arguments"] and '"id": "ts-stdio"' in card["arguments"]
    assert "entity.config.config" in card["hidden_keys"] and card["truncated"] is True


# ---- the doors with no park surface: call_tool with no context, and the MCP endpoint ------------------------------------------------------------------


@pytest.mark.parametrize("body, runs", [(SSE, True), ({**HTTP, "id": "ts-http-3"}, True), (STDIO, False), (PYTHON, False)], ids=["sse", "http", "stdio", "python"])
async def test_call_tool_with_no_context_evaluates_the_gate_like_the_direct_door(tmp_path, body, runs):
    """Review of #860, R860-3: with no session to park on (``invoke_one`` passes no context, as an out-of-loop dispatch does), ``call_tool``
    refused ANY enabled policy without evaluating it, so ``call_tool(system, create_toolset, <sse body>)`` was refused while
    ``system__create_toolset`` with the same body ran. It now evaluates the gate as the MCP endpoint does and refuses only when it says so."""
    import json

    from primer.agent.tool_manager import invoke_one

    sp, _registry, toolsets, _resolver = await _world(tmp_path, "fresh")
    try:
        result = await invoke_one(
            provider=toolsets["system"], tool_name="call_tool", principal=None,
            arguments={"toolset_id": "system", "tool_name": "create_toolset", "arguments": {"entity": body}},
        )
        stored = await sp.get_storage(Toolset).get(body["id"])
        if runs:
            assert not result.is_error, result.output
            assert stored is not None
        else:
            assert result.is_error and json.loads(result.output)["type"] == "approval-required", result.output
            assert stored is None
    finally:
        await sp.aclose()


@pytest.mark.parametrize("toolset_id, tool_name, arguments", [(t, n, a) for _, t, n, a, _ in GATED], ids=[c for c, *_ in GATED])
async def test_over_the_mcp_endpoint_an_admin_caller_is_refused_every_gated_write(tmp_path, toolset_id, tool_name, arguments):
    """R860-8: the MCP endpoint itself (``invoke_exposed``, with the tool allowlisted and an admin caller), not only its gate function."""
    from primer.mcp.dispatch import NotExposed, invoke_exposed
    from primer.mcp.exposure import ExposureDeps, update_exposure
    from primer.model.principal import Principal

    sp, registry, _toolsets, resolver = await _world(tmp_path, "fresh")
    try:
        deps = ExposureDeps(storage_provider=sp, provider_registry=registry, approval_resolver=resolver)
        scoped = f"{toolset_id}__{tool_name}"
        await update_exposure(enabled=True, allowed_tools=[scoped, "system__create_toolset"], updated_by="admin", deps=deps)
        admin = Principal(type="user", id="u-admin", display="admin", role="admin", source="local")
        before = await _state(sp)

        with pytest.raises(NotExposed) as refused:
            await invoke_exposed(scoped_id=scoped, arguments=arguments, principal=admin.id, actor=admin, deps=deps)

        assert refused.value.reason == "approval_required"
        assert await _state(sp) == before
        ran = await invoke_exposed(scoped_id="system__create_toolset", arguments={"entity": SSE}, principal=admin.id, actor=admin, deps=deps)
        assert not ran.is_error, ran.output
    finally:
        await sp.aclose()
