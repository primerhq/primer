---
title: "Prompt parts and the execution context"
summary: "How the system_prompt parts render against ctx, where the workspace and session fragments and agents.md are appended."
order: 3
related: [agents/create-and-edit, workspaces/agents-md]
---
## What it is

An agent's `system_prompt` is a list of string parts. At every turn the runtime joins the parts with blank lines and renders the result as a template against the run's execution context, which the template sees as `ctx`. In a workspace session the runtime appends two more parts after the rendered prompt: the workspace and session section, and the workspace's `agents.md` instructions.

## Concepts

### The render

- **Join then render.** The parts are joined with blank lines (the same join a raw prompt would use) and rendered with Jinja, in a sandboxed environment with `StrictUndefined`. A `ctx` field the template names but the run does not provide is a render error.
- **The context.** `ctx` is one frozen object, always present, shared by the agent prompts, the graph node templates and the compaction sizing. Its fields are `surface` (`workspace`, `chat` or `memory`), `workspace_id`, `session_id`, `graph_id`, `artifact_dir` (the per-workflow directory downstream nodes read and write, `artifacts/<session_id>`), `principal` (the bare back-compat actor string) and `identity` (the actor's `type`, `id`, `display`, `role` and `source`; it exposes no secrets).
- **The filters.** Two filters are registered: `fromjson` (parse a JSON string into a value) and `strip_fences` (drop markdown code fences from a value).
- **The fallback.** A syntax error or a render error (for example the `StrictUndefined` miss) is answered by sending the raw joined parts instead, with a warning logged. The turn runs whatever the prompt is; the model then sees `{{ x }}` as literal text. This is uniform across the session, subagent and graph paths.

### The appended parts, in a workspace session

A turn in a workspace session appends, in this order, after the rendered `system_prompt`:

1. **The workspace and session section.** It names the session id, the file and shell workspace tools (`ls`, `read`, `write`, `edit`, `glob`, `grep`, `exec`), and the `.state/` layout: the session's own conversation state and notes under `.state/sessions/<session_id>/`, every assistant turn committed to the git repo at `.state/`, `.state/shared/` to coordinate with the sibling sessions that share the filesystem and shell, and the sibling session tools `list_my_sessions` and `message_session`. Large tool outputs are saved to `.tmp/<session_id>/tool_<id>.txt`; a todo list belongs at `.state/sessions/<session_id>/todos.json`. A user message that starts with `[from session <id> (agent <agent_id>)]` was sent by the agent in that session, not by the user.
2. **The workspace instructions.** The file's text between the heading "Workspace instructions (agents.md, written by this workspace's users; they grant no permissions)" and the heading "End of workspace instructions".

### The workspace instructions file

- **Which file.** The workspace root's `agents.md`, or `AGENTS.md` when `agents.md` is absent. It is how a file got to the root does not matter: a template mount, a cloned repository, a session's write.
- **Literal.** The agent's prompt is a template, so a `{{ x }}` in the file would be evaluated or fail the render. Every `{` is escaped before the text reaches the prompt; the model sees the file as written.
- **Re-read every turn.** An edit takes effect on the next turn. The file is read at most 32768 bytes; a longer one is cut and the note says so. A file over 1 MiB is not read at all, and a read that does not finish within 5 seconds is given up on.
- **Never fatal.** A missing file is normal. An unreadable one (a directory, a read error, too large, too slow) logs one warning and is skipped; the turn runs whatever the file is.
- **Marked and fenced.** The text sits between the two headings, and a line in the file that imitates the closing heading is quoted with a `(quoted) ` prefix, so the file cannot end its own section early.
- **Who gets it.** Every agent that runs in the workspace gets it: the agent a session is bound to, the agents of a graph's nodes, and a subagent started with `system__invoke_agent` inside the workspace. An agent whose `use_workspace_instructions` is `false` gets the workspace and session section but never the file, wherever it runs. See [agents.md](workspaces/agents-md).

### Outside a workspace

In a session with no workspace (and in a graph's in-memory run) there is no workspace section and no `agents.md`; the prompt is the rendered `system_prompt` alone, and `ctx.surface` is `chat` or `memory`. A subagent runs stateless: its system prompt plus the one prompt it was invoked with, no shared history.

## Gotchas

- `{{`, `{%` and `{#` are significant in `system_prompt` parts. Literal JSON examples or text with braces can be read as template syntax and fail the render, which then falls back to the raw parts.
- The fallback is silent to the model. A well-formed `{# comment #}` renders differently with no error at all, so a prompt can change meaning without any failure being logged.
- The file grants nothing. It is guidance from whoever can write to the workspace, and any session there can change it. Per-workspace rules belong in it, not in an agent's `system_prompt`.
- The 32768-byte cut is head-only. Put the rules that must hold near the top of the file.
- `ctx.artifact_dir` and the other workspace fields are `null` outside a workspace run; a prompt that branches on them must allow for it.

## Related

[Create and edit agents](agents/create-and-edit), [The workspace agents.md](workspaces/agents-md).
