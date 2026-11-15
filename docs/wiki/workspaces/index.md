---
title: "Workspaces"
summary: "Create and manage workspaces: files, templates, providers, agents.md and the prime workspace."
order: 40
---

A workspace is the filesystem and git state that sessions share. These pages cover creating and deleting workspaces, templates and providers, the files you can reach from outside a session, the file tools every session has, agents.md, and the prime workspace. Start with [What a workspace is](workspaces/what-a-workspace-is).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`workspace_access__get_access`](reference/tools/workspace_access/get_access) | Who Workspace is shared with. | user |
| [`workspace_access__put_access`](reference/tools/workspace_access/put_access) | Change who Workspace is shared with (owners only). | user |
| [`workspace_events__list_workspace_events`](reference/tools/workspace_events/list_workspace_events) | Recent workspace-scoped tap events across all sessions (Studio activity backfill). | user |
| [`workspace_files__download_file`](reference/tools/workspace_files/download_file) | Download a file's raw bytes. | user |
| [`workspace_files__file_tree`](reference/tools/workspace_files/file_tree) | Return a one-level directory tree. | user |
| [`workspace_files__make_dir`](reference/tools/workspace_files/make_dir) | Create a directory (and any missing parents). | user |
| [`workspace_files__move_file`](reference/tools/workspace_files/move_file) | Move or rename a file or directory within the workspace. | user |
| [`workspace_log__workspace_log`](reference/tools/workspace_log/workspace_log) | Workspace state-repo git log. | user |
| [`workspace_log__workspace_show_commit`](reference/tools/workspace_log/workspace_show_commit) | Show one commit: header + per-file unified diff. | user |
| [`workspace_providers__find`](reference/tools/workspace_providers/find) | Find WorkspaceProvider with predicate. | admin |
| [`workspace_providers__get_access`](reference/tools/workspace_providers/get_access) | Who WorkspaceProvider is shared with. | user |
| [`workspace_providers__put_access`](reference/tools/workspace_providers/put_access) | Change who WorkspaceProvider is shared with (owners only). | user |
| [`workspace_providers__update`](reference/tools/workspace_providers/update) | Replace WorkspaceProvider. | admin |
| [`workspace_templates__find`](reference/tools/workspace_templates/find) | Find WorkspaceTemplate with predicate. | user |
| [`workspace_templates__get_access`](reference/tools/workspace_templates/get_access) | Who WorkspaceTemplate is shared with. | user |
| [`workspace_templates__put_access`](reference/tools/workspace_templates/put_access) | Change who WorkspaceTemplate is shared with (owners only). | user |
| [`workspaces_api__diagnostic_workspace`](reference/tools/workspaces_api/diagnostic_workspace) | Run a short read-only diagnostic command on a workspace. | user |
| [`workspaces_api__find_workspaces`](reference/tools/workspaces_api/find_workspaces) | Find Workspaces with predicate. | user |
| [`workspaces_api__rename_workspace`](reference/tools/workspaces_api/rename_workspace) | Rename a Workspace (set its human-readable label). | user |
| [`workspaces_api__set_workspace_events`](reference/tools/workspaces_api/set_workspace_events) | Set the workspace's lifecycle-event streaming config. | user |
| [`workspaces_api__set_workspace_terminal_access`](reference/tools/workspaces_api/set_workspace_terminal_access) | Grant or revoke non-admin access to the workspace's integrated terminal. | user |
