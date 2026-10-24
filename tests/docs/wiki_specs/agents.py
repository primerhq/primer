"""The seven agents feature pages (pivot P4, task 3).

The agents section documents what an agent row is and how its turn runs,
how to create and edit agents, how the prompt parts render, what a tool
grant means and when one session may drive another, how to invoke and
switch agents, and how a session compacts its own history.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("agents/what-an-agent-is", "What an agent is",
             "An agent is a stored row: a model profile, a system prompt, a tool grant and caps, run by the turn loop inside a session.",
             "feature", 1,
             related=("agents/create-and-edit", "agents/compaction", "sessions/what-a-session-is"),
             max_chars=5000),
    PageSpec("agents/create-and-edit", "Create and edit agents",
             "Create, list, get, find, update and delete agent rows, with the field checks, the seeded roster and the reset route.",
             "feature", 2,
             tools=scoped("crud", "create_agent update_agent")
             + scoped("system", "list_agents get_agent create_agent update_agent delete_agent find_agents"),
             related=("agents/prompts-and-parts", "agents/tools-and-grants", "providers/model-profiles"),
             max_chars=14000),
    PageSpec("agents/prompts-and-parts", "Prompt parts and the execution context",
             "How the system_prompt parts render against ctx, where the workspace and session fragments and agents.md are appended.",
             "feature", 3,
             related=("agents/create-and-edit", "workspaces/agents-md")),
    PageSpec("agents/tools-and-grants", "Tool grants and the rank rule",
             "What the tools list means, how scoped ids resolve, what a workspace session adds, and when one session may drive another.",
             "feature", 4,
             related=("agents/create-and-edit", "start-here/roles-and-approvals", "agents/invoke-an-agent")),
    PageSpec("agents/invoke-an-agent", "Invoke another agent",
             "Run a subagent on a prompt and take its text back, with the depth limit, the subagent identity and the cap error.",
             "feature", 5,
             tools=scoped("system", "invoke_agent"),
             related=("agents/switch-and-binding", "agents/compaction"),
             max_chars=12000),
    PageSpec("agents/switch-and-binding", "Switch agents and the binding",
             "Change which agent drives a session with switch_to_agent and switch_binding, which apply at the end of the current turn.",
             "feature", 6,
             tools=scoped("system", "switch_to_agent switch_binding"),
             related=("agents/invoke-an-agent", "sessions/what-a-session-is"),
             max_chars=12000),
    PageSpec("agents/compaction", "Compaction",
             "How a session compacts its own history before the prompt overflows the window, what the records say, and the 413 errors.",
             "feature", 7,
             related=("agents/what-an-agent-is", "agents/switch-and-binding")),
)
