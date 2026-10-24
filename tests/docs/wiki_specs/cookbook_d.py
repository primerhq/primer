"""The last four cookbook recipes: the research graph, discovery, MCP and prime (pivot P4, task 21).

One :class:`PageSpec` per recipe: the planned path, the exact frontmatter
(title, summary, order), the scoped tools the recipe calls and the related
paths. A recipe calls exactly the tools it lists, in a sound order
(:data:`RECIPE_ORDER`), and every JSON example validates against the live
schemas; ``tests/docs/test_wiki_pages.py`` checks all of it. The research
graph's body is also run through the real executor by
``tests/docs/test_cookbook_graph_runs.py``, which reads it from the page.
``run-a-task-through-prime`` shows ``workspace__write`` and
``workspaces__message_session`` as direct calls (``{"name": ...}``).
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("cookbook/multi-agent-graph-research", "Multi-agent research graph",
             "Build a researcher, fact-checker and writer graph with a loop back on failed sources, then run it.",
             "recipe", 13,
             tools=scoped("system", "create_agent create_collection create_graph get_graph") + scoped("workspaces", "create_workspace create_workspace_session get_workspace_session"),
             related=("graphs/create-and-edit", "graphs/what-a-graph-is"),
             must=("max_iterations", "on_max_iterations", "question"),
             max_chars=14000),
    PageSpec("cookbook/discover-a-capability", "Discover a capability",
             "Go from a vague task to the exact tool call: search the wiki, read a reference page and describe the tool.",
             "recipe", 14,
             tools=scoped("system", "wiki_search wiki_read describe_tool find_agents find_graphs"),
             related=("knowledge/the-platform-wiki", "toolsets/list-and-describe-tools"),
             max_chars=10000),
    PageSpec("cookbook/internal-tool-via-mcp", "Add an internal tool through MCP",
             "Register an MCP server as a toolset, check its tools and sign-in, and grant a tool to an agent.",
             "recipe", 15,
             tools=scoped("system", "create_toolset get_toolset list_toolset_tools"),
             related=("toolsets/mcp-toolsets", "agents/tools-and-grants"),
             max_chars=10000),
    PageSpec("cookbook/run-a-task-through-prime", "Run a task through prime",
             "Give the operator in prime a task: it creates a workspace, starts sessions, keeps a ledger and reports.",
             "recipe", 16,
             tools=scoped("workspaces", "list_workspace_templates create_workspace create_workspace_session") + scoped("workspace", "write") + scoped("workspaces", "find_sessions read_workspace_session_messages message_session") + scoped("system", "wait_for_event"),
             related=("workspaces/prime", "sessions/messaging-other-sessions"),
             max_chars=10000),
)
