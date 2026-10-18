"""One catalog of every tool an agent can call (pivot P1, task 5).

The platform has three kinds of agent-callable tool and, until now, no single place that lists them:

* the tools of the nine built-in toolsets (``primer.api.registries.provider_registry.RESERVED_TOOLSET_IDS``), run by ``system__call_tool`` from a
  ``(toolset_id, tool_name)`` pair;
* the seven SESSION file tools (``workspace__ls``, ``read``, ``write``, ``edit``, ``glob``, ``grep``, ``exec``): one set per workspace session, never a
  registry's, always on for an agent bound to a workspace;
* the CLIENT tool (``client__open_file``): the console's per-turn grant.

The last two are called DIRECTLY by their scoped id. ``call_tool`` resolves a toolset through the ``ProviderRegistry``, which has no ``workspace`` or
``client`` provider (they exist per session and per turn), so it cannot reach them: :data:`DIRECT_CALL_TOOLSETS`. What a page or a tool says about "how to
call this tool" follows from that split (:attr:`ToolEntry.via_call_tool`).

:class:`ToolEntry` is a tool plus what a reader needs that its descriptor does not say: the role that may run it, whether it pauses the turn, whether it
needs a workspace, and the approval gate that SHIPS on it. The shipped gate is :func:`default_gate`: a tool's LIVE gate is a row an operator edits, so a
versioned page cannot state it and ``system__describe_tool`` asks the ``ApprovalResolver`` instead.

Two readers build on it. :func:`collect_tool_entries` lists a set of providers (the wiki build over the built-in toolsets, boot time, in-process, never
blocks). :func:`find_tool_entry` and :func:`list_toolset_entries` look one tool or one toolset up through the registry, for a run in flight: a user-made
toolset can sit behind an unreachable MCP server, so that listing is bounded, and an OAuth consent request is the caller's to handle, not an answer of
"not found".

Imports are light on purpose (``primer.toolset.crud`` is imported inside :func:`default_gate`): ``primer.toolset._system_crud`` builds ``describe_tool`` on
this module, and ``crud`` imports ``_system_crud``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from primer.agent.tool_schemas import LIST_TIMEOUT_S, WORKSPACE_TOOL_ARGS, WORKSPACE_TOOLSET
from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool
from primer.model.except_ import AuthRequiredError, NotFoundError, PrimerError, ProviderError
from primer.toolset._describe import render_description
from primer.toolset.client import CLIENT_TOOLSET_ID, ClientToolsetProvider
from primer.workspace.local import tools as _local_tools

if TYPE_CHECKING:
    from primer.api.registries.provider_registry import ProviderRegistry

logger = logging.getLogger(__name__)

#: The scope of the seven session file tools (the tool manager's ``WORKSPACE_TOOLSET_ID``).
SESSION_TOOLSET_ID = WORKSPACE_TOOLSET
#: The scope of the console's client tool.
CLIENT_SCOPE_ID = CLIENT_TOOLSET_ID
#: The scopes whose tools are called by their scoped id, and are NOT reachable through ``call_tool``.
DIRECT_CALL_TOOLSETS = frozenset({SESSION_TOOLSET_ID, CLIENT_SCOPE_ID})

# The session tools are available to any authenticated user rank: the run is already inside a session the caller has open.
_USER_ROLE = "user"

# The local classes carry each tool's description, examples and Stop declaration; the argument models are the table the policy checks already use.
_SESSION_TOOL_CLASSES = (
    _local_tools.Edit, _local_tools.Exec, _local_tools.Glob, _local_tools.Grep, _local_tools.Ls, _local_tools.Read, _local_tools.Write,
)


@dataclass(frozen=True)
class ToolEntry:
    """One agent-callable tool, with the facts a reader needs besides its descriptor.

    ``tool`` is the descriptor as its provider lists it (``tool.id`` is the bare wire name, the ``tool_name`` of a ``call_tool``). ``default_gate`` is the approval
    gate that ships on the tool (``"required"``), or ``None``; see :func:`default_gate`.
    """

    toolset_id: str
    tool: Tool
    required_role: str
    pauses_turn: bool
    needs_workspace: bool
    default_gate: str | None

    @property
    def scoped_id(self) -> str:
        """``<toolset>__<tool>``, the name a model calls a direct tool by and the key the catalog is sorted on."""
        return f"{self.toolset_id}__{self.tool.id}"

    @property
    def via_call_tool(self) -> bool:
        """True when ``system__call_tool`` runs this tool; False for the session file tools and the client tool, which are called directly."""
        return self.toolset_id not in DIRECT_CALL_TOOLSETS


def session_tool_descriptors() -> list[Tool]:
    """The seven session file tools as descriptors, sorted by id: bare ids, ``toolset_id="workspace"``, role user, workspace required.

    Built from the same sources the tool manager reads when a session starts (the tool class's description and examples, its Stop and preview declarations, the argument
    model's schema), so the catalog and the model read one description.
    """
    return [
        Tool(
            id=cls.id,
            toolset_id=SESSION_TOOLSET_ID,
            description=render_description(cls.description, cls.examples),
            args_schema=WORKSPACE_TOOL_ARGS[cls.id].model_json_schema(),
            examples=list(cls.examples),
            requires_workspace=True,
            required_role=_USER_ROLE,
            interruptible=cls.interruptible,
            preview_args=cls.preview_args,
        )
        for cls in sorted(_SESSION_TOOL_CLASSES, key=lambda c: c.id)
    ]


async def client_tool_descriptors() -> list[Tool]:
    """The client toolset's tools (the console actions an agent may trigger), as its provider lists them."""
    return [tool async for tool in ClientToolsetProvider().list_tools()]


def default_gate(toolset_id: str, tool_name: str) -> str | None:
    """The approval gate that ships on a tool: ``"required"`` for the platform-construction (``crud``) tools, ``None`` for every other.

    ``ensure_crud_approval_policies`` (``primer/bootstrap/seed.py``) writes one unconditional policy per ``crud`` tool and nothing else is gated by default. An operator can
    disable or retune any of them, add others, and gate any tool, so this is what a fresh install does and not what a running one does: the live answer is the
    ``ApprovalResolver``'s (``system__describe_tool`` returns it).
    """
    from primer.toolset.crud import CRUD_TOOL_NAMES, CRUD_TOOLSET_ID

    return "required" if toolset_id == CRUD_TOOLSET_ID and tool_name in CRUD_TOOL_NAMES else None


def _entry(provider: ToolsetProvider, toolset_id: str, tool: Tool) -> ToolEntry:
    """One entry, with the role and the pause read the way ``call_tool`` and the tool manager read them (the provider's answer, else the descriptor's flag)."""
    return ToolEntry(
        toolset_id=toolset_id,
        tool=tool,
        required_role=provider.required_role(tool.id),
        pauses_turn=bool(provider.is_yielding(tool.id) or tool.yields),
        needs_workspace=bool(tool.requires_workspace),
        default_gate=default_gate(toolset_id, tool.id),
    )


def _session_entries() -> list[ToolEntry]:
    return [
        ToolEntry(
            toolset_id=SESSION_TOOLSET_ID,
            tool=tool,
            required_role=_USER_ROLE,
            pauses_turn=False,
            needs_workspace=bool(tool.requires_workspace),
            default_gate=default_gate(SESSION_TOOLSET_ID, tool.id),
        )
        for tool in session_tool_descriptors()
    ]


async def _client_entries() -> list[ToolEntry]:
    provider = ClientToolsetProvider()
    return [_entry(provider, CLIENT_SCOPE_ID, tool) async for tool in provider.list_tools()]


async def collect_tool_entries(toolset_providers: Mapping[str, ToolsetProvider]) -> list[ToolEntry]:
    """Every tool of every provider in ``toolset_providers`` (keyed by toolset id), plus the session and client tools, sorted by scoped id.

    A provider whose ``list_tools`` raises is skipped whole (a half-listed toolset would document tools the next build drops) with one WARNING that names it: one broken
    toolset must not hide the others.
    """
    entries: list[ToolEntry] = []
    for toolset_id, provider in toolset_providers.items():
        try:
            listed = [_entry(provider, toolset_id, tool) async for tool in provider.list_tools(principal=None)]
        except Exception as exc:  # noqa: BLE001 - a toolset that cannot list is skipped, not fatal
            logger.warning("tool catalog: toolset %r could not list its tools and is left out: %s: %s", toolset_id, type(exc).__name__, exc)
            continue
        entries.extend(listed)
    entries.extend(_session_entries())
    entries.extend(await _client_entries())
    return sorted(entries, key=lambda entry: entry.scoped_id)


async def list_toolset_entries(
    provider_registry: "ProviderRegistry", toolset_id: str, *, principal: str | None = None,
) -> list[ToolEntry]:
    """Every tool of ONE toolset, for a run in flight.

    The session and client scopes answer from their static descriptors. Any other id is resolved through ``provider_registry`` and listed under the
    ``LIST_TIMEOUT_S`` clock, as ``principal`` (the run's own, for a toolset that keeps per-user state such as an OAuth token cache; ``None`` is anonymous).

    Raises :class:`NotFoundError` for an unknown toolset (the registry's answer) and :class:`ProviderError` for one that does not list in time or fails with a bare
    exception; any other :class:`PrimerError` from the registry or the listing propagates as it is, and so does :class:`AuthRequiredError`: the consent URL
    it carries is for the person, so the caller must not read it as "no such tool".
    """
    if toolset_id == SESSION_TOOLSET_ID:
        return _session_entries()
    if toolset_id == CLIENT_SCOPE_ID:
        return await _client_entries()
    provider = await provider_registry.get_toolset(toolset_id)
    try:
        async with asyncio.timeout(LIST_TIMEOUT_S):
            return [_entry(provider, tool.toolset_id or toolset_id, tool) async for tool in provider.list_tools(principal=principal)]
    except PrimerError:
        raise
    except TimeoutError as exc:
        raise ProviderError(f"toolset {toolset_id!r} did not list its tools within {LIST_TIMEOUT_S:g} seconds", cause=exc) from exc
    except Exception as exc:  # noqa: BLE001 - an adapter that fails with a bare exception is a provider failure to the caller
        raise ProviderError(f"toolset {toolset_id!r} could not list its tools: {type(exc).__name__}: {exc}", cause=exc) from exc


async def find_tool_entry(
    provider_registry: "ProviderRegistry", toolset_id: str, tool_name: str, *, principal: str | None = None,
) -> ToolEntry | None:
    """The entry of tool ``tool_name`` in toolset ``toolset_id``, or ``None`` when there is none.

    ``None`` covers an unknown toolset, an unknown tool, and a toolset that cannot be listed (the last logged at WARNING). :class:`AuthRequiredError` is not
    folded into that: it propagates (see :func:`list_toolset_entries`). The session and client scopes are found without a registry.
    """
    try:
        entries = await list_toolset_entries(provider_registry, toolset_id, principal=principal)
    except AuthRequiredError:
        raise
    except NotFoundError:
        return None
    except PrimerError as exc:
        logger.warning("tool catalog: toolset %r could not be listed for %r: %s: %s", toolset_id, tool_name, type(exc).__name__, exc)
        return None
    return next((entry for entry in entries if entry.tool.id == tool_name), None)


__all__ = [
    "CLIENT_SCOPE_ID",
    "DIRECT_CALL_TOOLSETS",
    "SESSION_TOOLSET_ID",
    "ToolEntry",
    "client_tool_descriptors",
    "collect_tool_entries",
    "default_gate",
    "find_tool_entry",
    "list_toolset_entries",
    "session_tool_descriptors",
]
