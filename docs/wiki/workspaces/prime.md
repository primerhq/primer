---
title: "The prime workspace"
summary: "The permanent default workspace where the operator runs the platform: what it holds, why it cannot be deleted and how to reset its instructions."
order: 8
tools: [system__reset_prime_instructions]
related: [workspaces/agents-md, sessions/find-sessions, cookbook/run-a-task-through-prime]
---
## What it is
Prime is the default workspace: the one every user shares and the one the operator runs the platform from. It is the management plane. The work itself happens in other workspaces; prime is where you plan it, start it and follow it.

## Concepts
- Its id is not fixed. A fresh install creates it as `prime`. An install from before prime existed keeps the id its default workspace already had (usually `primer`) and shows it with the name `prime`. Find it by the one row with `is_default: true` in `workspaces__list_workspaces` or `workspaces__get_workspace`, never by its name or id: any workspace may be called `prime`, and prime may be renamed.
- It cannot be deleted. `workspaces__delete_workspace` answers `forbidden`, and the REST route answers 403 with `reserved_workspace`. Nothing is torn down and no session ends. Renaming it (`PATCH /v1/workspaces/{id}`) is allowed.
- Each person has their own main operator session in prime, bound to the `operator` agent and made on demand by `POST /v1/workspaces/{id}/main_session`. It is created as that person, so it runs at exactly their rank.
- The reach of prime. A session in prime reaches every workspace: it may create workspaces, start sessions in any of them, and find and message sessions anywhere. The rank rule still holds: you drive only a session at or below your own rank. A session in any other workspace reaches only its own workspace, so prime answers it `not-found`.
- The shipped instructions. At boot the platform writes its own `agents.md` to prime's root, but only when neither `agents.md` nor `AGENTS.md` is there. A boot never overwrites a file a person edited and never puts an `agents.md` beside an `AGENTS.md`. A deleted `agents.md` comes back at the next boot; an empty one is kept and adds nothing, so leave it empty to run prime with no instructions. How the file reaches agents is on [agents.md](workspaces/agents-md).
- What the shipped file says: prime is the management plane and the work happens in other workspaces; the operator keeps one ledger file per task under `tasks/` in prime, with the goal, where the work runs and what has happened so far; an agent whose own role says it never writes files leaves the ledger to the operator; read the platform wiki first when unsure.
- Anyone who can write files in prime can edit its `agents.md`, and an edit changes every agent in prime, every user's operator included. Treat the file as shared input, not as one only admins control.
- `system__reset_prime_instructions` puts the platform's copy back. It needs an admin and takes no arguments. It overwrites `agents.md` in prime whatever it holds, so the edits are lost, and it answers `{workspace_id, path, bytes, shadows}`.
  - It writes `agents.md` only. An `AGENTS.md` beside it stays, and is not read while `agents.md` exists: `shadows` is `"AGENTS.md"` then, else null.
  - A symlinked `agents.md` is written where the link points.
  - It runs from a session in prime or with no calling session. From a session outside prime it answers `forbidden` and writes nothing.
  - With no default workspace yet it answers `not-found`. When prime does not answer within 10 seconds (to reach it, then to write) it answers `unavailable`, and a write given up on may still land, with the same shipped text.
  - The REST twin is `POST /v1/setup/reset_prime_instructions`.

## How to
1. Find prime: list the workspaces and take the row with `is_default: true`.
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
- Never hard-code prime's id. Look for `is_default: true`.
- A reset throws away every edit to prime's `agents.md`. Ask the user before you call it, and to change the instructions rather than restore them, write the file instead.
- An edit to prime's `agents.md` reaches every agent in prime from its next turn. Keep it short and ask the user before you change it.
- A session outside prime cannot see prime at all: every workspace tool answers `not-found` for it, and `workspaces__list_workspaces` leaves it out.

## Related
[agents.md](workspaces/agents-md), [find sessions](sessions/find-sessions), [run a task through prime](cookbook/run-a-task-through-prime).
