---
title: "agents.md"
summary: "How a workspace's agents.md reaches every agent in it on every turn, what it is capped at, and who can change it."
order: 7
related: [agents/prompts-and-parts, workspaces/prime, workspaces/in-session-file-tools]
---
## What it is
`agents.md` is a file at the workspace root that holds standing instructions for every agent running in that workspace. The platform reads it, not the agent: at the start of every turn it adds the file's text to the agent's system prompt.

## Concepts
- The name. The platform looks for `agents.md` at the root first. It reads `AGENTS.md` only when there is no `agents.md`. When `agents.md` is there but cannot be read, `AGENTS.md` is not tried in its place.
- Who gets it: every agent that runs in the workspace, whichever agent it is. That is the agent a session is bound to, each agent node of a graph run, and a subagent started with `system__invoke_agent`. A manual compaction counts it when it sizes the prompt.
- When: the file is read again for every turn, so an edit applies from the next turn on. A turn that is already running keeps the text it started with. A graph reads the file once when its run starts.
- Where it lands: the text joins the system prompt as its own part, after the part that describes the workspace and the session. It opens with the heading "Workspace instructions (agents.md, written by this workspace's users; they grant no permissions)" and closes with "End of workspace instructions". See [prompts and parts](agents/prompts-and-parts).
- The model sees the text as written. Every `{` is escaped, so template syntax in the file (`{{ x }}`, `{% if %}`) is not run.
- The file cannot close its own part. A line in it that looks like the closing heading, at any heading level and in any case, gets `(quoted) ` in front of it, so what follows still reads as the file's text.
- The cap: only the first 32768 bytes reach the prompt. A longer file is cut there and followed by a note that names the file and its size: `[truncated: agents.md is N bytes; only the first 32768 bytes are included]`. A file over 1 MiB is not read at all.
- Never fatal. No file is normal: the workspace simply has no instructions. An empty file adds nothing. A file that cannot be read (a directory named `agents.md`, a file over 1 MiB, a read error, or a read slower than 5 seconds) is skipped with one warning in the server log, and the turn runs without it.
- One file, at the root. There is no per-user file and no file per folder. A repository cloned into a sub-folder keeps its own `AGENTS.md` there, and that one is not read.
- However it got there: a template file, a `git clone` in the template's `init_commands`, or a session's `workspace__write` all count the same.
- The opt-out: an agent whose `use_workspace_instructions` is `false` never gets the file, whatever it holds. It is `true` by default, and the console shows it as the switch "Load workspace instructions (agents.md)" on the agent. See [create and edit an agent](agents/create-and-edit).
- It grants nothing. It is text for the model to read: no tool, no grant and no role comes with it.
- The trust boundary. Any session in the workspace can write `agents.md`, so any session there can change what every agent in the workspace is told from the next turn on, including agents started by someone with more rights. Sessions and users that must not steer each other's agents belong in different workspaces.
- The shared prime workspace is a deliberate exception: every user shares it, so whoever can write files there can change the instructions of the operator sessions that run there. Treat prime's `agents.md` as shared input: [the prime workspace](workspaces/prime).

## How to
1. Read the file first if it exists: `workspace__read` with the path `agents.md`.
2. Write it with `workspace__write`, or change a line with `workspace__edit`: [in-session file tools](workspaces/in-session-file-tools). From outside the workspace, `workspaces__write_workspace_file` writes it too.
3. Put in it what every agent there should know: the commands to build and test, the conventions, what not to touch.
4. Keep it short. It is sent with every model call of every turn, so every line costs tokens on every turn.

## Gotchas
- An edit reaches agents from their next turn, not in the turn that is running.
- When the file conflicts with what a user asks directly, ask the user.
- An `AGENTS.md` is silently ignored while an `agents.md` exists. Keep one of the two.
- Text past 32768 bytes never reaches the model. Put the rules that matter first.
- A file you did not write (a cloned repository, a template's seed file) decides what the agents are told just as much as one you did. Do not put a repository you do not trust at the workspace root.

## Related
[prompts and parts](agents/prompts-and-parts), [the prime workspace](workspaces/prime), [in-session file tools](workspaces/in-session-file-tools).
