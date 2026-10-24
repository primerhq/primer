"""The workspaces pages (pivot P4, task 5).

Eight feature pages: what a workspace is, create and delete, the files and
the log from outside a session, templates, providers (admin), the seven
in-session file tools, agents.md, and the prime workspace.
``list_workspace_provider_choices`` is conditional on the templates page: it
is shown when live.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("workspaces/what-a-workspace-is", "What a workspace is",
             "A workspace is the filesystem and git state that sessions share: providers, templates, the .state and .tmp layout and the lifecycle phases.",
             "feature", 1,
             related=("workspaces/create-and-delete", "workspaces/templates", "sessions/what-a-session-is")),
    PageSpec("workspaces/create-and-delete", "Create and delete a workspace",
             "Create a workspace from a template, look it up, list them, and delete one (which destroys its files and ends its sessions).",
             "feature", 2,
             tools=scoped("workspaces", "list_workspaces get_workspace create_workspace delete_workspace"),
             related=("workspaces/templates", "workspaces/prime", "sessions/start-a-session"),
             must=("reserved_workspace",)),
    PageSpec("workspaces/files-and-log", "Files and the log",
             "Read, write, list and delete files in any workspace from outside its sessions, and read the workspace log.",
             "feature", 3,
             tools=scoped("workspaces", "list_workspace_files get_workspace_file_info read_workspace_file write_workspace_file delete_workspace_file get_workspace_log"),
             related=("workspaces/in-session-file-tools", "workspaces/what-a-workspace-is")),
    PageSpec("workspaces/templates", "Workspace templates",
             "Reusable workspace recipes: list, read, create, change and delete templates, and which settings need an admin.",
             "feature", 4,
             tools=scoped("workspaces", "list_workspace_templates get_workspace_template create_workspace_template update_workspace_template delete_workspace_template"),
             tools_if_present=scoped("workspaces", "list_workspace_provider_choices"),
             related=("workspaces/create-and-delete", "workspaces/providers"),
             max_chars=12000),
    PageSpec("workspaces/providers", "Workspace providers",
             "Admin: register the compute backends (local, container, Kubernetes) that workspaces are built on.",
             "feature", 5,
             audience="admin",
             tools=scoped("workspaces", "list_workspace_providers get_workspace_provider create_workspace_provider delete_workspace_provider"),
             related=("workspaces/templates", "workspaces/what-a-workspace-is")),
    PageSpec("workspaces/in-session-file-tools", "In-session file tools",
             "The seven file and shell tools every session has without asking: ls, read, write, edit, glob, grep and exec.",
             "feature", 6,
             tools=scoped("workspace", "ls read write edit glob grep exec"),
             related=("workspaces/files-and-log", "workspaces/agents-md"),
             must=("`access`", "`writes`", "same directory"),
             max_chars=12000),
    PageSpec("workspaces/agents-md", "agents.md",
             "How a workspace's agents.md reaches every agent in it on every turn, what it is capped at, and who can change it.",
             "feature", 7,
             related=("agents/prompts-and-parts", "workspaces/prime", "workspaces/in-session-file-tools"),
             must=("agents.md", "AGENTS.md", "32768", "every turn")),
    PageSpec("workspaces/prime", "The prime workspace",
             "The permanent default workspace where the operator runs the platform: what it holds, why it cannot be deleted and how to reset its instructions.",
             "feature", 8,
             tools=scoped("system", "reset_prime_instructions"),
             related=("workspaces/agents-md", "sessions/find-sessions", "cookbook/run-a-task-through-prime"),
             must=("prime", "tasks/", "reserved_workspace")),
)
