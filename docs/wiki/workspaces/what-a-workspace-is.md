---
title: "What a workspace is"
summary: "A workspace is the filesystem and git state that sessions share: providers, templates, the .state and .tmp layout and the lifecycle phases."
order: 1
related: [workspaces/create-and-delete, workspaces/templates, sessions/what-a-session-is]
---
## What it is
A workspace is a filesystem plus a git repository of state, and every session in it shares both. A session runs inside exactly one workspace: its files are the session's world for reading, writing and running commands. A workspace outlives its sessions, so one workspace can host many sessions, one after another or at the same time.

## Concepts
- Three records make up the surface:
  - A provider (`WorkspaceProvider`) is the compute backend: `local` (a folder on the server), `container` (Docker, Podman or containerd) or `kubernetes` (a pod with a volume). Registering one needs an admin: [workspace providers](workspaces/providers).
  - A template (`WorkspaceTemplate`) is the recipe: a `provider_id`, a `backend` block that matches the provider's kind, `files` to seed, `env`, `init_commands` and `resources`. See [workspace templates](workspaces/templates).
  - A workspace (`Workspace`) is the live instance built from a template.
- The fields of a workspace you read most: `id`; `name`, an optional label; `template_id`, where it came from; `template_snapshot`, the full template as it was at create; `provider_id`; `overrides`, the per-workspace changes it was created with; `phase`; `failure_reason`; and `reply_binding`, the channel its sessions' questions, approvals and updates go to ([reply binding](channels/reply-binding)). The workspace tools add `is_default` to every row they serve: `true` only on the default workspace, [prime](workspaces/prime).
- The snapshot is what the workspace runs on. Changing or deleting its template later never changes or breaks it.
- `phase` is one of `pending`, `running`, `failed` or `terminating`:
  - A row starts `pending` and becomes `running` at its first good health probe.
  - Three missed probes in a row turn `running` into `failed`, fill `failure_reason`, and end every open session in it with `workspace_lost`, which is permanent.
  - Three good probes in a row bring a `failed` workspace back to `running`. The sessions it lost stay ended.
  - `terminating` is in the model, but nothing writes it today: a delete is synchronous.
- A failed create leaves nothing behind. The row is written last, and a failure after the live instance exists tears that instance down again. There is no half-built workspace to clean up.
- The layout under the workspace root:
  - The root holds your files, in any layout. One name is special: `agents.md`, the standing instructions of every agent there ([agents.md](workspaces/agents-md)).
  - `.state/` is a git repository the platform commits to. `.state/sessions/<sid>/` holds each session's history (`messages.jsonl`, `session.json`). `.state/shared/` is the place for files that sessions share to coordinate.
  - `.tmp/` is untracked scratch. `.tmp/<sid>/` holds the large tool outputs a session saved instead of returning in full.
  - A template can move both trees (`state_path`, `tmp_path`); the defaults are `.state` and `.tmp`.
- Two ways in:
  - Inside a session, the seven [in-session file tools](workspaces/in-session-file-tools) (`workspace__read`, `workspace__exec` and the rest) are bound to the session's workspace and take no `workspace_id`.
  - From outside, the `workspaces__*` tools take a `workspace_id`: [files and the log](workspaces/files-and-log), [create and delete a workspace](workspaces/create-and-delete).
- The reach rule. A session reaches only its own workspace with every tool that names a workspace or a session: another workspace answers `not-found`, exactly like an id that does not exist. A session in prime, and a caller with no session (a person or an API client over MCP), reach every workspace. A session outside prime cannot create a workspace at all.
- Sessions in one workspace share its files and its shell. Two sessions that write the same file race. Coordinate through a file under `.state/shared/`, or send a message: [messaging other sessions](sessions/messaging-other-sessions).

## Gotchas
- `.state/` and `.tmp/` belong to the runtime. From outside a session, a caller who is not an admin cannot read them, and nobody writes or deletes there with the `workspaces__*` file tools. Read a session's transcript with `workspaces__read_workspace_session_messages`, not the raw file.
- `.tmp/` is not tracked. Do not keep anything there that you need later.
- The default workspace's id is not fixed: a fresh install calls it `prime`, an older one keeps its old id. Find it by `is_default: true`, never by its name or id. See [the prime workspace](workspaces/prime).
- Deleting a workspace deletes its files and every session's history with it. Ask the user first.
- A `failed` workspace has already ended its open sessions. Start new sessions only once it is `running` again, or create a fresh workspace.

## Related
[create and delete a workspace](workspaces/create-and-delete), [workspace templates](workspaces/templates), [what a session is](sessions/what-a-session-is).
