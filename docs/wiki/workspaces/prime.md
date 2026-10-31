---
title: "The prime workspace"
summary: "The permanent default workspace where the operator runs the platform: what it holds, why it cannot be deleted and how to reset its instructions."
order: 8
tools: [system__reset_prime_instructions]
related: [workspaces/agents-md, sessions/find-sessions, cookbook/run-a-task-through-prime]
---
## What it is
Prime is the install's first default workspace: the first admin's own default, and the one the operator runs the platform from. It is the management plane. Every other full user gets a private default workspace of their own (`ws-<user id>`) with an operator session on it at their first sign-in, and reaches prime only when an owner shares it. The work itself happens in other workspaces; prime is where you plan it, start it and follow it.

## Concepts
- Its id is not fixed. A fresh install creates it as `prime`. An install from before prime existed keeps the id its default workspace already had (usually `primer`) and shows it with the name `prime`. `is_default: true` in `workspaces__list_workspaces` and `workspaces__get_workspace` marks the caller's own default workspace: prime for the first admin, your own private workspace for anyone else. Never find a workspace by its name or id: any workspace may be called `prime`, and prime may be renamed.
- It cannot be deleted, and neither can any user's default workspace. `workspaces__delete_workspace` answers `forbidden`, and the REST route answers 403 with `reserved_workspace` (to an owner; anyone else is refused by access first). Nothing is torn down and no session ends. Renaming it (`PATCH /v1/workspaces/{id}`) is allowed.
- Each person with full access to prime has their own main operator session in it, bound to the `operator` agent and made on demand by `POST /v1/workspaces/{id}/main_session` (full access needed). It is created as that person, so it runs at exactly their rank.
- Prime grants no reach. A run acts with its person's access wherever it lives: it reads the workspaces that person may read and acts in the ones they hold full access to, from prime or from any other workspace. A person who holds no access to prime gets `not-found` for it, as for an unknown id. See [Sharing and access](start-here/sharing-and-access).
- The shipped instructions. At boot the platform writes its own `agents.md` to prime's root, but only when neither `agents.md` nor `AGENTS.md` is there. A boot never overwrites a file a person edited and never puts an `agents.md` beside an `AGENTS.md`. A deleted `agents.md` comes back at the next boot; an empty one is kept and adds nothing, so leave it empty to run prime with no instructions. How the file reaches agents is on [agents.md](workspaces/agents-md).
- What the shipped file says: prime is the control room and you are the overseer; the work happens in other workspaces, and you do not build things in prime. You create one workspace per task and name it after the task, and each session gets a brief that stands alone. You create the task's ledger file under `tasks/` in prime and keep it current with the in-session file tools (`workspace__write` and `workspace__edit`): the goal, the workspace, the status, the sessions and a log. You answer "how is it going?" from the live sessions, report a session that waits for a person to the user (you cannot answer for them), wait for a session to end instead of polling, and ask the user first before you delete a workspace or touch another agent's session. When you are unsure how to do something, read the wiki before you act.
- Anyone who can write files in prime can edit its `agents.md`, and an edit changes every agent in prime, every user's operator included. Treat the file as shared input, not as one only admins control.
- `system__reset_prime_instructions` puts the platform's copy back. It needs an admin and takes no arguments. It overwrites `agents.md` in prime whatever it holds, so the edits are lost, and it answers `{workspace_id, path, bytes, shadows}`.
  - It writes `agents.md` only. An `AGENTS.md` beside it stays, and is not read while `agents.md` exists: `shadows` is `"AGENTS.md"` then, else null.
  - A symlinked `agents.md` is written where the link points.
  - The person behind the call also needs full access to prime (an admin holds it on every workspace), from any session or with no calling session. Without it the call answers `forbidden` and writes nothing.
  - With no default workspace yet it answers `not-found`. When the deployment refuses prime (a local workspace where the deployment forbids one) it answers `conflict`, and when a directory called `agents.md` is in the way, `bad-request`. When prime does not answer within 10 seconds (to reach it, then to write) it answers `unavailable`, and a write given up on may still land, with the same shipped text.
  - The REST twin is `POST /v1/setup/reset_prime_instructions`.

## How to
1. Find your default workspace: list the workspaces and take the row with `is_default: true` (prime only for the first admin).
2. For a new task, create a workspace for the work, start its sessions there, and keep the task's ledger file under `tasks/` in prime: [run a task through prime](cookbook/run-a-task-through-prime).
3. Follow the work from prime with `workspaces__find_sessions` and `workspaces__message_session`: [find sessions](sessions/find-sessions).
4. If prime's instructions were edited into a broken state, ask the user first, then reset them (needs an admin).

## Tool calls
### Reset prime's instructions
Overwrites `agents.md` in prime with the platform's shipped copy. Needs an admin.

```json
{"toolset_id": "system", "tool_name": "reset_prime_instructions", "arguments": {}}
```

Reference: [`system__reset_prime_instructions`](reference/tools/system/reset_prime_instructions)

## Gotchas
- Do not build in prime. Create a workspace for the work and run its sessions there.
- Never hard-code prime's id. `is_default: true` names the caller's own default workspace, which is prime only for the first admin.
- A reset throws away every edit to prime's `agents.md`. Ask the user before you call it, and to change the instructions rather than restore them, write the file instead.
- An edit to prime's `agents.md` reaches every agent in prime from its next turn. Keep it short and ask the user before you change it.
- A person with no access to prime cannot see it at all, wherever their session runs: every workspace tool answers `not-found` for it, and `workspaces__list_workspaces` leaves it out.

## Related
[agents.md](workspaces/agents-md), [find sessions](sessions/find-sessions), [run a task through prime](cookbook/run-a-task-through-prime).
