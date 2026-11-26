"""Review 2026-11-23 (MCP gated listing): a tool whose approval policy is
statically required is withheld from discovery, while a tool whose policy
an LLM judge decides per call must stay listed.

``authorized_catalogue`` applies only the gate it can decide without a
model call: the ``required`` type refuses every call, so advertising it
could only answer ``tool_unavailable``. LLM-judge and Rego policies are
evaluated on the call path only (``call_tool`` keeps deciding per call),
because a listing that called an LLM would be a surprise cost on every
catalogue request. Discovery and dispatch agree, and an ungated tool is
still listed and still callable.
"""

from __future__ import annotations

import pytest

from primer.mcp.dispatch import NotExposed, invoke_exposed
from primer.mcp.exposure import (
    ExposureDeps,
    authorized_catalogue,
    update_exposure,
)
from primer.model.chat import Tool, ToolCallResult
from primer.model.principal import Principal
from primer.model.tool_approval import (
    LlmApprovalConfig,
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)

ADMIN = Principal(type="user", id="u", display="u", role="admin", source="local")


class _SysProvider:
    """Minimal ToolsetProvider stand-in for the reserved ``system`` id."""

    toolset_id = "system"

    def __init__(self, tools: list[Tool], roles: dict[str, str]) -> None:
        self._tools = tools
        self._roles = roles
        self.calls: list[str] = []

    async def list_tools(self, *, principal=None):
        del principal
        for tool in self._tools:
            yield tool

    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        del arguments, principal, ctx
        self.calls.append(tool_name)
        return ToolCallResult(output="ok", is_error=False)

    def is_yielding(self, tool_name: str) -> bool:
        del tool_name
        return False

    def required_role(self, tool_name: str) -> str:
        return self._roles.get(tool_name, "admin")


class _Registry:
    def __init__(self, providers: dict[str, _SysProvider]) -> None:
        self._providers = providers

    async def get_toolset(self, toolset_id: str):
        return self._providers.get(toolset_id)


class _StubResolver:
    """An ApprovalResolver that gates exactly the (toolset, tool) pairs it
    was handed: ``required`` for the gated set, an LLM judge for the
    llm_gated set -- the storage shape the real resolver reads, without a
    database in between."""

    def __init__(self, gated, llm_gated=()) -> None:
        self._gated = set(gated)
        self._llm_gated = set(llm_gated)

    async def find(self, *, toolset_id: str, tool_name: str):
        if (toolset_id, tool_name) in self._gated:
            return ToolApprovalPolicy(
                id="tool-approval-policy-47",
                toolset_id=toolset_id,
                tool_name=tool_name,
                approval=RequiredApprovalConfig(),
            )
        if (toolset_id, tool_name) in self._llm_gated:
            return ToolApprovalPolicy(
                id="tool-approval-policy-48",
                toolset_id=toolset_id,
                tool_name=tool_name,
                approval=LlmApprovalConfig(
                    provider_id="judge-prov",
                    model="judge-model",
                    prompt="approve or deny",
                ),
            )
        return None


def _system_deps(storage, gated, llm_gated=(), extra_tools=()) -> tuple[ExposureDeps, _SysProvider]:
    tools = [
        Tool(
            id="ping", toolset_id="system",
            description="Ungated liveness probe.",
            args_schema={"type": "object", "properties": {}},
        ),
        Tool(
            id="create_agent", toolset_id="system",
            description="Create an agent.",
            args_schema={"type": "object", "properties": {}},
        ),
        Tool(
            id="create_llm_provider", toolset_id="system",
            description="Create an LLM provider.",
            args_schema={"type": "object", "properties": {}},
        ),
        *extra_tools,
    ]
    provider = _SysProvider(
        tools,
        {"ping": "restricted", "create_agent": "user", "create_llm_provider": "admin"},
    )
    deps = ExposureDeps(
        storage_provider=storage,
        provider_registry=_Registry({"system": provider}),
        approval_resolver=_StubResolver(gated, llm_gated=llm_gated),
    )
    return deps, provider


async def _catalogue_ids(deps: ExposureDeps) -> set[str]:
    await update_exposure(
        enabled=True, allowed_tools=[], updated_by="admin", deps=deps,
    )
    items = await authorized_catalogue(
        deps, actor=ADMIN, principal="u", api_token_scopes=None,
    )
    return {item.scoped_id for item in items}


@pytest.mark.asyncio
async def test_an_approval_gated_tool_is_withheld_from_discovery(fake_storage_provider):
    """A tool whose effective approval policy is required must not be
    advertised: the catalogue skips it, and the call that would have
    used the advertised id is refused the same way it always was."""
    deps, provider = _system_deps(fake_storage_provider, gated={("system", "create_agent")})

    ids = await _catalogue_ids(deps)

    assert ids == {"system__ping", "system__create_llm_provider"}, (
        f"the approval-gated tool was advertised: {sorted(ids)}"
    )
    with pytest.raises(NotExposed) as raised:
        await invoke_exposed(
            scoped_id="system__create_agent", arguments={},
            principal="u", actor=ADMIN, deps=deps,
        )
    assert raised.value.reason == "approval_required"
    assert provider.calls == []  # the handler never runs


@pytest.mark.asyncio
async def test_ungated_tools_are_still_listed_and_callable(fake_storage_provider):
    """The tightening must not hide what the gate lets through: with no
    approval policy at all the whole role-cleared catalogue is listed,
    and a listed tool still calls."""
    deps, provider = _system_deps(fake_storage_provider, gated=set())

    ids = await _catalogue_ids(deps)

    assert ids == {"system__ping", "system__create_agent", "system__create_llm_provider"}
    result = await invoke_exposed(
        scoped_id="system__create_agent", arguments={},
        principal="u", actor=ADMIN, deps=deps,
    )
    assert result.is_error is False
    assert provider.calls == ["create_agent"]


@pytest.mark.asyncio
async def test_an_llm_gated_tool_is_listed_and_listing_makes_no_llm_call(
    fake_storage_provider, monkeypatch,
):
    """Discovery must not call an LLM judge (review round 1): a tool whose
    policy is an LLM judge stays listed, the listing itself makes zero
    judge calls, and the call path still consults the judge exactly once,
    per call, as before."""
    import primer.agent.approval as approval

    judge_calls: list[tuple[str, str]] = []

    async def _fake_judge(*, policy, context, provider_registry, storage_provider=None):
        del provider_registry, storage_provider
        judge_calls.append((policy.model, context.tool_name))
        return {"required": False, "reason": "fake judge approves"}

    monkeypatch.setattr(approval, "_dispatch_llm_judge", _fake_judge)

    judge_tool = Tool(
        id="send_message", toolset_id="system",
        description="Send a message (LLM-judged).",
        args_schema={"type": "object", "properties": {}},
    )
    deps, provider = _system_deps(
        fake_storage_provider,
        gated=set(),
        llm_gated={("system", "send_message")},
        extra_tools=[judge_tool],
    )

    ids = await _catalogue_ids(deps)

    assert ids == {
        "system__ping", "system__create_agent",
        "system__create_llm_provider", "system__send_message",
    }, f"the LLM-gated tool must stay listed: {sorted(ids)}"
    assert judge_calls == [], f"discovery called the LLM judge: {judge_calls!r}"

    # The call path still decides per call through the judge.
    result = await invoke_exposed(
        scoped_id="system__send_message", arguments={},
        principal="u", actor=ADMIN, deps=deps,
    )
    assert result.is_error is False
    assert provider.calls == ["send_message"]
    assert judge_calls == [("judge-model", "send_message")], (
        f"the call path must consult the judge exactly once: {judge_calls!r}"
    )


class _FailsOnSecondEnumeration(_SysProvider):
    """Enumerates until ``failing`` is set, then fails: the hidden-target audit lookup must not raise."""

    def __init__(self, tools, roles) -> None:
        super().__init__(tools, roles)
        self.failing = False
        self.failed = 0

    async def list_tools(self, *, principal=None):
        if self.failing:
            self.failed += 1
            raise RuntimeError("toolset enumeration failed")
        async for tool in super().list_tools(principal=principal):
            yield tool


@pytest.mark.asyncio
async def test_a_toolset_failing_enumeration_leaves_the_hidden_target_reasonless(fake_storage_provider):
    """Review round 2: ``hidden_rejection_reason`` enumerates the toolset again; a failure there means "no
    reason known" (``call_tool`` answers tool_unavailable), never an exception out of ``call_tool``."""
    from primer.mcp.exposure import build_routing_map, hidden_rejection_reason

    deps, _ = _system_deps(fake_storage_provider, gated={("system", "create_agent")})
    flaky = _FailsOnSecondEnumeration(
        [Tool(id="create_agent", toolset_id="system", description="x", args_schema={"type": "object"})],
        {"create_agent": "user"},
    )
    deps = ExposureDeps(
        storage_provider=fake_storage_provider,
        provider_registry=_Registry({"system": flaky}),
        approval_resolver=deps.approval_resolver,
    )
    await update_exposure(enabled=True, allowed_tools=[], updated_by="admin", deps=deps)
    # The routing map is built (and cached) while the toolset still enumerates: the target routes.
    assert "system__create_agent" in await build_routing_map(deps, reserved_only=True)
    flaky.failing = True

    reason = await hidden_rejection_reason(
        deps, actor=ADMIN, principal="u", api_token_scopes=None, scoped_id="system__create_agent",
    )

    assert reason is None
    assert flaky.failed == 1  # the lookup did enumerate again, and swallowed the failure


@pytest.mark.asyncio
async def test_a_failing_routing_map_leaves_the_hidden_target_reasonless(fake_storage_provider, monkeypatch):
    from primer.mcp import exposure
    from primer.mcp.exposure import hidden_rejection_reason

    deps, _ = _system_deps(fake_storage_provider, gated=set())
    await update_exposure(enabled=True, allowed_tools=[], updated_by="admin", deps=deps)

    async def boom(*args, **kwargs):
        raise RuntimeError("routing failed")

    monkeypatch.setattr(exposure, "build_routing_map", boom)
    reason = await hidden_rejection_reason(
        deps, actor=ADMIN, principal="u", api_token_scopes=None, scoped_id="system__ping",
    )
    assert reason is None
