"""The collaboration section (wiki-first W5, workstream WS-G).

Two topics: the dictated file-based coordinator and sub-agents pattern
(G1, the OQ-1 file shape default: one task-assignment file plus one
response file per sub-agent) and the other evidence-backed coordination
surfaces (open scope). The wiki-driven agent recipe (G2) is speced with
the agents section, where its page lives.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("collaboration", "Collaboration",
             "How agents work together in one install: the file-based coordinator and sub-agents pattern, and the other coordination surfaces.",
             "section", 180),
    PageSpec("collaboration/coordinator-pattern", "Coordinator and sub-agents",
             "One coordinator session hands out tasks through a file and waits with watch_files; each sub-agent answers in its own response file.",
             "feature", 1,
             tools=scoped("workspace", "ls read write") + scoped("workspace_ext", "watch_files"),
             related=("collaboration/other-patterns", "utilities/sleep-and-watch-files",
                      "workspaces/in-session-file-tools", "sessions/messaging-other-sessions"),
             must=("ONE response file per sub-agent", "<agent-id>.status.md", "re-calls",
                   "parked_on", ".state/shared/", "initial_instructions"),
             max_chars=9000),
    PageSpec("collaboration/other-patterns", "Other collaboration patterns",
             "The other ways agents coordinate: cross-session messaging, trigger and subscription events, and in-turn delegation to agents and graphs.",
             "feature", 2,
             tools=scoped("workspaces", "message_session")
             + scoped("system", "wait_for_event")
             + scoped("trigger", "create_subscription fire_now")
             + scoped("system", "invoke_agent")
             + scoped("workspace_ext", "invoke_graph"),
             related=("collaboration/coordinator-pattern", "sessions/messaging-other-sessions",
                      "triggers/subscriptions", "agents/invoke-an-agent", "triggers/waiting-for-events"),
             must=("hop limit", "steer_outranks_fire", "scope of this page is open",
                   "fails the child graph", "parked call"),
             max_chars=9000),
)
