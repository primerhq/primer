"""``workspace_ext`` reserved internal toolset - workspace-session-only yields.

This toolset groups the context-heavy, workspace-oriented yielding tools
that an agent should only ever see while running inside a **workspace
session**. It is reserved (its id ``workspace_ext`` short-circuits the
normal :class:`~primer.model.provider.Toolset` row lookup in
:class:`primer.api.registries.ProviderRegistry`) and built once at app
startup.

It is also **special**: although an agent can bind ``workspace_ext`` on
its Tools tab like any other toolset, its tools are registered into the
agent's live tool context ONLY when the agent runs in a workspace
session. When the same agent is invoked on a CHAT, the
``workspace_ext`` tools are filtered out at the resolution choke point
(:meth:`primer.agent.tool_manager.ToolExecutionManager.list_tools`) so
they never enter the chat's context window. This is the whole point:
keep these high-token, session-bound tools out of chat context.

Tool catalog (5 tools, all yielding)
------------------------------------

* ``sleep``                - pause the turn for a fixed duration
  (moved from ``misc``).
* ``watch_files``          - park until a watched workspace path changes
  (moved from ``workspaces``).
* ``invoke_graph``         - run a child graph inside this session
  (moved from ``workspaces``).
* ``subscribe_to_trigger`` - park the session until a trigger fires
  (moved from ``trigger``).
* ``subscribe_to_channel_event`` - park the session until a matching
  channel event fires (moved from ``trigger``).

The BARE tool ids are unchanged by the move - only the scoped id
(``toolset_id__bare``) changes (e.g. ``misc__sleep`` ->
``workspace_ext__sleep``). Yield event keys, resume hooks, and the chat
soft-yield set are all keyed on the bare name and are therefore
untouched. The handlers, argument models, and resume hooks continue to
live in their original modules; this module only re-homes the tool
descriptors under the new toolset id.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from primer.model.chat import Tool, ToolExample
from primer.toolset._describe import make_tool
from primer.toolset.internal import InternalToolsetProvider, ToolHandler

# Handlers + argument models are reused verbatim from their original
# modules. Moving a tool between toolsets changes only the scoped id; the
# handler, arg schema, yield event key, and resume hook stay put.
from primer.toolset.misc import _SleepArgs, _sleep_handler
from primer.toolset.workspaces import (
    _InvokeGraphArgs,
    _WatchFilesArgs,
    _invoke_graph_handler,
    _watch_files_handler,
)
from primer.toolset.trigger import (
    TOOL_SUBSCRIBE,
    TOOL_SUBSCRIBE_CHANNEL,
    _make_subscribe_channel_handler,
    _make_subscribe_handler,
)


if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)


WORKSPACE_EXT_TOOLSET_ID = "workspace_ext"


def build_workspace_ext_toolset(
    *,
    storage_provider: "StorageProvider",
    toolset_id: str = WORKSPACE_EXT_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the immutable ``workspace_ext`` toolset.

    ``storage_provider`` is required only by the ``subscribe_to_trigger``
    handler (it writes the one-shot ``parked_session`` Subscription row);
    the other three handlers read everything they need from the
    :class:`~primer.model.yield_.ToolContext` at call time.
    """
    registry: dict[str, tuple[Tool, ToolHandler]] = {
        "sleep": (
            make_tool(
                id="sleep",
                toolset_id=toolset_id,
                purpose=(
                    "Pause this agent turn for ``seconds`` seconds "
                    "(fractional allowed); returns ``{requested_seconds, "
                    "elapsed_seconds}``."
                ),
                when=(
                    "Use when you must wait a fixed duration (polling with "
                    "backoff, deliberate pacing); not for waiting on a human "
                    "(use ``ask_user``)."
                ),
                args_schema=_SleepArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={"seconds": 5},
                        returns="resumes after 5s",
                        note="yielding; worker released",
                    ),
                ],
                yields=True,
                required_role="user",
            ),
            _sleep_handler,
        ),
        "watch_files": (
            make_tool(
                id="watch_files",
                toolset_id=toolset_id,
                purpose=(
                    "Watch one or more workspace-relative paths and pause "
                    "this agent turn until something changes. Returns "
                    "``{timed_out, changes}`` on change/timeout or "
                    "``{cancelled, ...}`` if the operator skipped the yield. "
                    "Each change carries ``{path, event_type, mtime_after}``."
                ),
                when=(
                    "Use when you must block until a watched path changes; not "
                    "for a one-shot listing (use ``list_workspace_files``). "
                    "Paths must be workspace-relative (no absolute, no ``..``). "
                    "Optional ``timeout_seconds`` (global yield cap) and "
                    "``batch_window_ms`` (default 250) coalesces change bursts."
                ),
                args_schema=_WatchFilesArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={"paths": ["src/main.py"]},
                        returns="{timed_out: false, changes: [...]}",
                        note="yielding; parks until a watched path changes",
                    ),
                    ToolExample(
                        args={
                            "paths": ["src"],
                            "timeout_seconds": 30,
                            "batch_window_ms": 500,
                        },
                    ),
                ],
                yields=True,
                required_role="user",
            ),
            _watch_files_handler,
        ),
        "invoke_graph": (
            make_tool(
                id="invoke_graph",
                toolset_id=toolset_id,
                purpose=(
                    "Run another graph inside the current workspace session "
                    "and get its output text. The invoked graph's state nests "
                    "under this session."
                ),
                when=(
                    "Use when you need to delegate a self-contained multi-step "
                    "workflow to a graph from within a session; for a single "
                    "agent use invoke_agent."
                ),
                args_schema=_InvokeGraphArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={
                            "graph_id": "graph-review",
                            "input": "Review the diff.",
                        },
                        returns="{output: <graph output text>}",
                        note="runs a subgraph in this session; can park on HITL",
                    ),
                ],
                yields=True,
                required_role="user",
            ),
            _invoke_graph_handler,
        ),
        "subscribe_to_trigger": (
            # Re-home the existing descriptor under the new toolset id; the
            # bare id, args schema, and yield flag are preserved.
            TOOL_SUBSCRIBE.model_copy(update={"toolset_id": toolset_id}),
            _make_subscribe_handler(storage_provider),
        ),
        "subscribe_to_channel_event": (
            # Channel-event variant: parks a workspace session until a
            # matching channel event fires; keep it out of chat context too.
            TOOL_SUBSCRIBE_CHANNEL.model_copy(update={"toolset_id": toolset_id}),
            _make_subscribe_channel_handler(storage_provider),
        ),
    }

    logger.info(
        "workspace_ext toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )
    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["WORKSPACE_EXT_TOOLSET_ID", "build_workspace_ext_toolset"]
