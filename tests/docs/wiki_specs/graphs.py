"""The graphs pages (pivot P4, task 6).

Four user-tier feature pages. ``graphs/what-a-graph-is`` owns no tool and
carries the drift guard for the node and edge kinds. ``graphs/create-and-edit``
owns the gated ``crud`` pair and the six ``system`` graph tools.
``graphs/run-a-graph`` owns ``workspace_ext__invoke_graph``.
``graphs/threads`` owns the six ``system`` graph-thread tools.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("graphs/what-a-graph-is", "What a graph is",
             "A graph is nodes and edges that run agents, tools and sub-graphs in steps: node kinds, edge kinds and node output.",
             "feature", 1,
             related=("graphs/create-and-edit", "graphs/run-a-graph", "agents/what-an-agent-is"),
             must=("max_iterations",),
             max_chars=12000),
    PageSpec("graphs/create-and-edit", "Create and edit a graph",
             "Write a graph with its templates and routers, validate it, and manage graphs with the graph tools.",
             "feature", 2,
             tools=scoped("crud", "create_graph update_graph") + scoped("system", "list_graphs get_graph create_graph update_graph delete_graph find_graphs"),
             related=("graphs/what-a-graph-is", "graphs/run-a-graph", "approvals/policies"),
             must=("StrictUndefined", "initial_input"),
             max_chars=14000),
    PageSpec("graphs/run-a-graph", "Run a graph",
             "Run a graph in a session or as a step of another session, pass it input, answer its questions and read its output.",
             "feature", 3,
             tools=scoped("workspace_ext", "invoke_graph"),
             related=("sessions/start-a-session", "human-in-the-loop/ask-user", "graphs/threads"),
             must=("graph_input",)),
    PageSpec("graphs/threads", "Graph threads",
             "A graph thread records one run of a graph: its steps, node states and how it ended. Read and clean them up.",
             "feature", 4,
             tools=scoped("system", "list_graph_threads get_graph_thread create_graph_thread update_graph_thread delete_graph_thread find_graph_threads"),
             related=("graphs/run-a-graph", "sessions/observe-a-session")),
)
