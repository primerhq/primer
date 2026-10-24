"""The first four cookbook recipes (pivot P4, task 18).

One :class:`PageSpec` per recipe: the planned path, the exact frontmatter
(title, summary, order), the scoped tools the recipe calls and the related
paths. A recipe calls exactly the tools it lists, in a sound order
(:data:`RECIPE_ORDER`), and every JSON example validates against the live
schemas; ``tests/docs/test_wiki_pages.py`` checks all of it.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("cookbook/create-and-run-a-session", "Create and run a session",
             "Run an existing agent in a fresh workspace and collect what it wrote.",
             "recipe", 1,
             tools=scoped("workspaces", "list_workspace_templates create_workspace create_workspace_session get_workspace_session read_workspace_file delete_workspace"),
             related=("sessions/start-a-session", "sessions/observe-a-session"),
             max_chars=10000),
    PageSpec("cookbook/monitor-and-resume-a-parked-session", "Monitor and resume a parked session",
             "Find out why a session is parked, then steer, resume, stop or cancel it.",
             "recipe", 2,
             tools=scoped("workspaces", "get_workspace_session read_workspace_session_messages steer_workspace_session resume_workspace_session interrupt_workspace_session cancel_workspace_session"),
             related=("sessions/control-a-session", "sessions/observe-a-session"),
             max_chars=10000),
    PageSpec("cookbook/run-a-graph-and-collect-results", "Run a graph and collect results",
             "Pick a graph, run it in a session and read its output.",
             "recipe", 3,
             tools=scoped("system", "list_graphs get_graph") + scoped("workspaces", "create_workspace create_workspace_session get_workspace_session read_workspace_session_messages"),
             related=("graphs/run-a-graph", "sessions/start-a-session"),
             max_chars=10000),
    PageSpec("cookbook/workspace-as-build-env", "A workspace as a build environment",
             "Use a throwaway workspace as a reproducible build, run a build agent in it and collect logs and artifacts.",
             "recipe", 4,
             tools=scoped("workspaces", "create_workspace_template") + scoped("system", "create_agent") + scoped("workspaces", "create_workspace create_workspace_session get_workspace_session get_workspace_log read_workspace_file delete_workspace"),
             related=("workspaces/templates", "workspaces/files-and-log"),
             max_chars=10000),
)
