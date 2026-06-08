"""Tool envelope for workspace-scoped tools.

Three exports:

* :class:`WorkspaceTool` -- ABC every workspace tool implements
  (``ls``, ``read``, ``write``, ``edit``, ``glob``, ``grep``, ``exec``
  in v1; concrete implementations land in
  :mod:`primer.workspace.tools` under sub-project C).
* :class:`ToolCallContext` -- per-call context the agent runtime hands
  the tool's ``execute()``.
* :class:`ToolResult` -- what a tool returns: the string the LLM sees
  plus optional metadata and a truncation cache pointer.

The runtime is responsible for wrapping every ``execute()`` with arg
validation and outer truncation -- see the "Tool dispatch wrapping"
section of the design spec.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` for the
full design.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


if TYPE_CHECKING:
    # AgentSession is concrete and lives at primer/workspace/session.py,
    # which lands in sub-project D. Until then, the forward reference
    # below stays as a string so this module is importable without it.
    from primer.model.chat import ToolExample
    from primer.workspace.session import AgentSession


# ===========================================================================
# Tool result
# ===========================================================================


class ToolResult(BaseModel):
    """What a workspace tool returns from its ``execute()`` call.

    The agent runtime applies outer truncation to ``output`` unless the
    tool already truncated -- see the spec's "Tool dispatch wrapping"
    section. Tools that page their own output (e.g. ``read`` with
    ``limit``) opt out of outer truncation by setting
    ``truncated=True`` and supplying ``output_path``.
    """

    output: str = Field(
        ...,
        description="The string the LLM sees as the tool result.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-tool extras for the harness / UI. Not always shown to "
            "the model; intended for telemetry, attachments, etc."
        ),
    )
    truncated: bool = Field(
        default=False,
        description=(
            "True if the tool already truncated its own output and "
            "the runtime should NOT apply outer truncation. When True, "
            "``output_path`` SHOULD be set."
        ),
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "When truncated, absolute path to the cached full output "
            "under ``.tmp/<session_id>/``. ``None`` when not truncated."
        ),
    )


# ===========================================================================
# Tool call context
# ===========================================================================


class ToolCallContext(BaseModel):
    """What the runtime hands a tool's ``execute()``.

    Carries the workspace / session / agent / call identifiers so the
    tool can attribute its writes to the right state slot, plus the
    cooperative cancel signal and the live :class:`AgentSession` handle
    (so tools that need to cache large output or commit additional
    state can do so directly).

    The two callback fields (``metadata_callback``, ``ask_callback``)
    are optional; the runtime supplies them when the tool needs to
    push mid-execution UI updates or request permission decisions.
    The permission-request / decision protocol is not yet designed
    (see spec rev 3 -- approvals are a future concern); for now
    ``ask_callback`` is typed as ``Callable[[Any], Awaitable[Any]]``
    and will be tightened once the protocol lands.
    """

    workspace_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1)
    call_id: str = Field(
        ...,
        min_length=1,
        description="The model-supplied tool call id.",
    )
    abort: asyncio.Event = Field(
        ...,
        exclude=True,
        description=(
            "Cooperative cancellation signal. Tools SHOULD check it at "
            "convenient yield points and exit promptly when set."
        ),
    )
    session: "AgentSession" = Field(
        ...,
        exclude=True,
        description=(
            "Live :class:`AgentSession` handle. Tools that need to "
            "cache large output or commit additional state call into "
            "the session directly."
        ),
    )
    metadata_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = Field(
        default=None,
        exclude=True,
        description=(
            "Optional callback for pushing mid-execution metadata "
            "updates to the harness / UI."
        ),
    )
    # NOTE: the request / decision shapes for the approval protocol
    # are TBD per spec rev 3 ("approvals are a future concern"). When
    # that protocol lands, tighten the type from Any to the proper
    # PermissionRequest / PermissionDecision pair.
    ask_callback: Callable[[Any], Awaitable[Any]] | None = Field(
        default=None,
        exclude=True,
        description=(
            "Optional callback for requesting a permission decision "
            "from the user. Request / response shapes TBD when the "
            "approval protocol is designed."
        ),
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)


# ===========================================================================
# Workspace tool ABC
# ===========================================================================


class WorkspaceTool(ABC):
    """One tool that operates on a workspace's filesystem / shell.

    Concrete subclasses (``Ls``, ``Read``, ``Write``, ``Edit``,
    ``Glob``, ``Grep``, ``Exec``) ship in :mod:`primer.workspace.tools`
    under sub-project C. Each defines a Pydantic ``parameters()`` model
    for argument validation and an ``execute(args, ctx)`` body that
    actually does the work.
    """

    id: ClassVar[str]
    """Short, stable, LLM-facing tool name."""

    description: ClassVar[str]
    """Long-form markdown description loaded into the agent's system
    prompt; tells the model when and how to use the tool."""

    examples: ClassVar[list["ToolExample"]] = []
    """Structured worked examples rendered into the LLM-facing description."""

    requires_workspace_context: ClassVar[bool] = True
    """When True, the runtime refuses to dispatch this tool unless the
    calling agent is attached to a workspace session. Default True
    because every workspace tool needs a session for state attribution
    and tmp-cache scoping."""

    @abstractmethod
    def parameters(self) -> type[BaseModel]:
        """Return the Pydantic class describing this tool's arguments.

        The runtime validates the model-supplied args against this
        schema before calling :meth:`execute`; validation errors are
        formatted back to the LLM for retry.
        """

    @abstractmethod
    async def execute(self, args: BaseModel, ctx: ToolCallContext) -> ToolResult:
        """Run the tool.

        ``args`` is an already-validated instance of
        ``self.parameters()``. The implementation is responsible for
        honouring ``ctx.abort`` at convenient yield points; the
        runtime applies outer truncation to the returned
        :attr:`ToolResult.output` unless the tool sets
        :attr:`ToolResult.truncated` itself.
        """


__all__ = [
    "ToolCallContext",
    "ToolResult",
    "WorkspaceTool",
]
