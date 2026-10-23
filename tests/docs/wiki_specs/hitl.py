"""The human-in-the-loop pages (pivot P4, task 10).

One :class:`PageSpec` per page of the section: ``ask-user`` (the pausing
question), ``inform-user`` (the one-way update) and ``open-file`` (the file
the user can see in their console). All three are user-audience feature
pages with their single tool, in the section's order.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("human-in-the-loop/ask-user", "Ask the user",
             "Pause the turn and ask a person a question, with an optional answer schema and timeout.",
             "feature", 1,
             tools=scoped("system", "ask_user"),
             related=("human-in-the-loop/inform-user", "channels/reply-binding", "sessions/control-a-session"),
             must=("response_schema",)),
    PageSpec("human-in-the-loop/inform-user", "Inform the user",
             "Send a person a one-way update and keep working.",
             "feature", 2,
             tools=scoped("misc", "inform_user"),
             related=("human-in-the-loop/ask-user", "channels/reply-binding")),
    PageSpec("human-in-the-loop/open-file", "Open a file for the user",
             "Open a workspace file in the user's console without waiting.",
             "feature", 3,
             tools=scoped("client", "open_file"),
             related=("human-in-the-loop/inform-user", "workspaces/in-session-file-tools")),
)
