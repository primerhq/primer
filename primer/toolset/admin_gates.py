"""The default approval gates on the admin-tier platform writes (ticket 01a16a29, the admin half of audit G6-1).

#851 gates every door to the agent, graph and trigger writes (:mod:`primer.toolset.crud`: the crud tools and ``CRUD_TWINS``). A gate binds
whatever the run's role, but the writes below had none, so a run at an admin's rank (the seeded operator's main session usually runs at its
creator's) walked around the approval system:

* :data:`POLICY_WRITES`: create, update and delete of a tool approval policy (the system toolset is the only one with them). A model could delete
  or relax the very gate that protects a write, then make the write.
* :data:`HOST_CODE_TOOLSET_WRITES`: the system toolset's ``create_toolset`` and ``update_toolset``, gated when the BODY is a python toolset (its
  source runs on the server host) or a stdio MCP toolset (it launches a command there): the twin of ``crud::create_python_toolset``, which is gated.
* :data:`HARNESS_LIFECYCLE`: harness install, sync and uninstall, which create, update and delete a bundle's agents, graphs, triggers and toolsets.

:data:`ADMIN_GATES` maps each to the gate a fresh install ships: ``required``, except the two toolset writes, whose gate is the Rego policy
:data:`HOST_CODE_TOOLSET_POLICY`. Each is decided by an admin only (``approvers`` = ``primer.session.approvers.ADMIN_ONLY_SPEC``, through
``crud.shipped_approvers``): a person who may not make the write, or a chat-platform user primer cannot identify, cannot approve it. ``bootstrap/seed.py`` seeds them with the crud gates (``crud.DEFAULT_GATED_TOOLS`` lists every default gate);
migration 16 gives an existing install the same rows. Like every policy they gate model-initiated calls: the tool manager, ``system__call_tool``
(the harness tools' one door in a run, as their bare ids carry ``__`` and the tool manager will not mount them) and the MCP endpoint, which has no
park and refuses a gated call as ``approval_required``. A person acting over REST is not gated.

The toolset gate: a Rego policy that is an allowlist
-----------------------------------------------------
The gate reads the body of the call, and lets it through only when ``arguments.entity`` is EXACTLY an http or sse MCP toolset
(``provider == "mcp"`` and ``config.transport`` ``"http"`` or ``"sse"``) or an internal one (``provider == "internal"``). Everything else parks:
python, stdio, and any body the policy does not recognise (a provider or transport in another case or missing, a body that is not an object, a
provider type added later). The handler parses the same body with ``Toolset.model_validate``, which takes the enum values exactly and has no
aliases, so a body the gate lets through is one the handler either refuses or stores as a toolset that runs nothing on the host: no spelling of
python or stdio passes (``tests/toolset/test_admin_run_gates.py`` pins it on a corpus of spellings). The condition reads only the body, never the
stored row: an update that turns an http toolset into a stdio or python one parks, and one that turns a stdio toolset into an http one does not
(it starts nothing). The key is the system toolset's, under every spelling of its id (``ApprovalResolver.find`` reads ``_system`` as ``system``).

A Rego row, not a check in the gate: the condition stays visible and editable where the operator manages every other gate (to gate every
toolset write, replace it with a ``required`` policy), and the three gate sites run it unchanged. If the Rego engine cannot run, the gate fails
closed and every toolset write parks.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from primer.common.preview_paths import missing_paths
from primer.model.tool_approval import PolicyApprovalConfig, RequiredApprovalConfig

if TYPE_CHECKING:
    from primer.model.chat import Tool
    from primer.toolset.internal import ToolHandler


# The toolsets' ids, spelled out: this module is imported by the toolsets it names (the completeness test pins them against the live builds).
_SYSTEM = "system"
_HARNESS = "harness"

#: The tool approval policy writes. A gate an admin-run model can delete is no gate.
POLICY_WRITES: tuple[tuple[str, str], ...] = (
    (_SYSTEM, "create_tool_approval_policy"),
    (_SYSTEM, "update_tool_approval_policy"),
    (_SYSTEM, "delete_tool_approval_policy"),
)

#: The toolset writes that take a whole body, gated by :data:`HOST_CODE_TOOLSET_POLICY` when it runs code on the server host.
HOST_CODE_TOOLSET_WRITES: tuple[tuple[str, str], ...] = (
    (_SYSTEM, "create_toolset"),
    (_SYSTEM, "update_toolset"),
)

#: The harness operations that write a bundle's agents, graphs, triggers and toolsets (the bare ids carry the ``harness__`` prefix).
HARNESS_LIFECYCLE: tuple[tuple[str, str], ...] = (
    (_HARNESS, "harness__install"),
    (_HARNESS, "harness__sync"),
    (_HARNESS, "harness__uninstall"),
)

#: The shipped gate on ``system::create_toolset`` and ``system::update_toolset`` (see the module docstring). Migration 16 keeps its own copy of
#: this text, as it shipped.
HOST_CODE_TOOLSET_POLICY = """package primer.tool_approval

# Shipped on system::create_toolset and system::update_toolset (ticket 01a16a29). A python toolset runs its source on the server
# host and a stdio MCP toolset launches a command there, so a write whose body is either pauses for approval. An allowlist: only a
# body that is exactly an http or sse MCP toolset, or an internal one, runs ungated. Any other body parks, including a provider or
# transport spelled another way or missing. To gate every toolset write, replace this policy with a required one.

default required := true

default reason := "a python toolset runs its source, and a stdio MCP toolset launches a command, on the server host"

required := false if {
    input.arguments.entity.provider == "mcp"
    input.arguments.entity.config.transport in {"http", "sse"}
}

required := false if {
    input.arguments.entity.provider == "internal"
}
"""

#: Each tool's shipped gate, as a model to copy: ``required`` but for the two toolset writes.
ADMIN_GATES: Mapping[tuple[str, str], RequiredApprovalConfig | PolicyApprovalConfig] = MappingProxyType({
    **{key: RequiredApprovalConfig() for key in POLICY_WRITES},
    **{key: PolicyApprovalConfig(policy=HOST_CODE_TOOLSET_POLICY) for key in HOST_CODE_TOOLSET_WRITES},
    **{key: RequiredApprovalConfig() for key in HARNESS_LIFECYCLE},
})


# What the approval card of each may show (design note 01a11cd3-66b0): what is created, changed or removed, and the code a person approves (review
# of #860, R860-4 and R860-5): a policy's Rego source (code, rarely a secret) and a stdio toolset's command. Withheld, as on the crud cards: the free
# text that can hold a pasted secret (a judge prompt, a stdio env, a toolset's URL and headers) and a python source, whose card draws its fingerprint
# instead. "Show all" on the card still returns the whole call to whoever may decide it.
_POLICY_PREVIEW = (
    "entity.id", "entity.toolset_id", "entity.tool_name", "entity.enabled", "entity.approval.type", "entity.approval.policy", "entity.timeout_seconds",
    "entity.approvers",
)
_TOOLSET_PREVIEW = (
    "entity.id", "entity.provider", "entity.config.transport", "entity.config.config.command", "entity.config.default_timeout_seconds",
    "entity.config.image", "entity.config.allow_network",
)
PREVIEW_ARGS: Mapping[tuple[str, str], tuple[str, ...]] = MappingProxyType({
    (_SYSTEM, "create_tool_approval_policy"): _POLICY_PREVIEW,
    (_SYSTEM, "update_tool_approval_policy"): ("id", *_POLICY_PREVIEW),
    (_SYSTEM, "delete_tool_approval_policy"): ("id",),
    (_SYSTEM, "create_toolset"): _TOOLSET_PREVIEW,
    (_SYSTEM, "update_toolset"): ("id", *_TOOLSET_PREVIEW),
    # An install or sync names the overrides it applies, and the person who approves it sees them (ticket 01a16a29, R860-1).
    (_HARNESS, "harness__install"): ("id", "overrides"),
    (_HARNESS, "harness__sync"): ("id", "overrides"),
    (_HARNESS, "harness__uninstall"): ("id",),
})

# What each card draws FIRST (``Tool.preview_lead``), before its one-line preview cuts a long entity, and never cut itself: what the person
# decides on. A policy write leads with whether the gate is on, its type and its Rego (and, on an update, whether each changes the stored policy:
# a call that disables or neutralises a gate never reads like a no-op; review of #860, R2-1); a toolset write with its stdio command and the
# fingerprint of its python source, which the allowlist withholds (``sha256 <12 hex>, <N> lines``, computed when the call parks); a harness
# install or sync with its overrides, keys sorted. Every text, list or object value carries its fingerprint.
_POLICY_LEAD = ("entity.enabled", "entity.approval.type", "entity.approval.policy")
_TOOLSET_LEAD = ("entity.config.config.command", "entity.config.source")
PREVIEW_LEAD: Mapping[tuple[str, str], tuple[str, ...]] = MappingProxyType({
    (_SYSTEM, "create_tool_approval_policy"): _POLICY_LEAD,
    (_SYSTEM, "update_tool_approval_policy"): _POLICY_LEAD,
    (_SYSTEM, "create_toolset"): _TOOLSET_LEAD,
    (_SYSTEM, "update_toolset"): _TOOLSET_LEAD,
    (_HARNESS, "harness__install"): ("overrides",),
    (_HARNESS, "harness__sync"): ("overrides",),
})


def apply_admin_gate_previews(toolset_id: str, registry: "dict[str, tuple[Tool, ToolHandler]]") -> None:
    """Give each admin-gated tool of ``toolset_id`` in ``registry`` the approval card :data:`PREVIEW_ARGS` and :data:`PREVIEW_LEAD` declare,
    checked against its schema."""
    for (owner, bare), paths in PREVIEW_ARGS.items():
        if owner != toolset_id or bare not in registry:
            continue
        tool, handler = registry[bare]
        lead = PREVIEW_LEAD.get((owner, bare))
        gone = missing_paths(tool.args_schema, (*paths, *(lead or ())))
        if gone:
            raise ValueError(f"{toolset_id} tool {bare!r}: preview paths {gone} name no argument of its schema")
        registry[bare] = (tool.model_copy(update={"preview_args": paths, "preview_lead": lead}), handler)


__all__ = [
    "ADMIN_GATES",
    "HARNESS_LIFECYCLE",
    "HOST_CODE_TOOLSET_POLICY",
    "HOST_CODE_TOOLSET_WRITES",
    "POLICY_WRITES",
    "PREVIEW_ARGS",
    "PREVIEW_LEAD",
    "apply_admin_gate_previews",
]
