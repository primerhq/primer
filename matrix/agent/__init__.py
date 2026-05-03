"""Agent runtime — executors, compaction, tool dispatch, streaming taps.

Public surface added by sub-project F1 (Foundation):

* :class:`ToolExecutionManager` -- central tool dispatcher.
* :func:`tail_split` -- compaction helper for splitting history into
  ``(head, tail)``.
* :data:`DEFAULT_COMPACTION_PROMPT` -- system-default compaction
  instructions used when an agent has no
  :attr:`Agent.compaction_prompt`.
* :class:`AgentEventSubscriber`, :class:`Subscription` -- streaming-tap
  scaffolding.

F2/F3/F4 add :class:`CompactionStrategy`, :class:`AgentExecutor`, and
:class:`WorkspaceAgentExecutor` (plus their dependencies).
"""

from matrix.agent.base import _BaseAgentExecutor
from matrix.agent.compaction import (
    DEFAULT_CONTEXT_LIMIT,
    MODEL_CONTEXT_FALLBACK,
    CompactedTurn,
    CompactionStrategy,
    lookup_context_length,
)
from matrix.agent.events import (
    AgentEventSubscriber,
    Subscription,
)
from matrix.agent.executor import AgentExecutor
from matrix.agent.prompts import DEFAULT_COMPACTION_PROMPT
from matrix.agent.workspace_executor import WorkspaceAgentExecutor
from matrix.agent.tail import tail_split
from matrix.agent.tool_manager import (
    WORKSPACE_TOOLSET_ID,
    ToolExecutionManager,
)


__all__ = [
    "AgentEventSubscriber",
    "AgentExecutor",
    "CompactedTurn",
    "CompactionStrategy",
    "DEFAULT_COMPACTION_PROMPT",
    "DEFAULT_CONTEXT_LIMIT",
    "MODEL_CONTEXT_FALLBACK",
    "Subscription",
    "ToolExecutionManager",
    "WORKSPACE_TOOLSET_ID",
    "WorkspaceAgentExecutor",
    "_BaseAgentExecutor",
    "lookup_context_length",
    "tail_split",
]
