"""The sessions pages (pivot P4, task 4).

One :class:`PageSpec` per page of the section: what a session is (the states,
the record, where it runs), starting one, observing one, controlling one,
messaging another session and finding sessions in any workspace. The first
page carries the drift guard for the session states; the others are the home
pages of the session tools of the ``workspaces`` toolset.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("sessions/what-a-session-is", "What a session is",
             "A session is one agent or graph working inside a workspace: its states, where it runs and where its record lives.",
             "feature", 1,
             related=("sessions/start-a-session", "sessions/observe-a-session", "workspaces/what-a-workspace-is"),
             must=("autonomous", "last_turn_error")),
    PageSpec("sessions/start-a-session", "Start a session",
             "Start an agent or graph in a workspace with a brief, and decide whether it runs at once.",
             "feature", 2,
             tools=scoped("workspaces", "create_workspace_session"),
             related=("sessions/observe-a-session", "graphs/run-a-graph", "workspaces/create-and-delete"),
             must=("autonomous", "auto_start")),
    PageSpec("sessions/observe-a-session", "Observe a session",
             "List sessions, read one session's state and transcript, and tap live events.",
             "feature", 3,
             tools=scoped("workspaces", "list_workspace_sessions get_workspace_session read_workspace_session_messages workspace_tap"),
             related=("sessions/control-a-session", "sessions/find-sessions", "triggers/waiting-for-events")),
    PageSpec("sessions/control-a-session", "Control a session",
             "Pause, resume, steer, stop, restart or cancel a session, and what each one does to a running turn.",
             "feature", 4,
             tools=scoped("workspaces", "pause_workspace_session resume_workspace_session steer_workspace_session interrupt_workspace_session cancel_workspace_session restart_workspace_session"),
             related=("sessions/messaging-other-sessions", "human-in-the-loop/ask-user", "approvals/policies"),
             must=("interrupt_requested",)),
    PageSpec("sessions/messaging-other-sessions", "Messaging other sessions",
             "List the sessions in your workspace and send one a message that steers it or starts its next turn.",
             "feature", 5,
             tools=scoped("workspaces", "list_my_sessions message_session"),
             related=("sessions/control-a-session", "sessions/find-sessions", "triggers/waiting-for-events"),
             must=("[from session", "hop-limit", "rate-limited", "if_idle", "never the answer")),
    PageSpec("sessions/find-sessions", "Find sessions",
             "Find sessions in any workspace by workspace, agent, state or who started them.",
             "feature", 6,
             tools=scoped("workspaces", "find_sessions"),
             related=("sessions/observe-a-session", "sessions/messaging-other-sessions", "workspaces/prime")),
)
