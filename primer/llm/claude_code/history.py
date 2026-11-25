"""The claude_code history split and replay (spec 6.4).

The shared agent-runtime helpers (``primer.llm.agent_runtime.history``),
re-exported so the old import paths keep working.
"""

from primer.llm.agent_runtime.history import (
    ReplayPlan,
    plan_replay,
    replay_tool_id,
    split_tail,
)

__all__ = ["ReplayPlan", "plan_replay", "replay_tool_id", "split_tail"]
