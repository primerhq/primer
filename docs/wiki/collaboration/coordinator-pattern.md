---
title: "Coordinator and sub-agents"
summary: "One coordinator session hands out tasks through a file and waits with watch_files; each sub-agent answers in its own response file."
order: 1
tools: [workspace__ls, workspace__read, workspace__write, workspace_ext__watch_files]
related: [collaboration/other-patterns, utilities/sleep-and-watch-files, workspaces/in-session-file-tools, sessions/messaging-other-sessions]
---
## What it is
One workspace holds a single COORDINATOR session and several sub-agent sessions. The coordinator hands out tasks through a task-assignment file and waits for answers with a file watch; each sub-agent works from the assignment file and writes its answer to its own response file. The coordination medium is files: no new primitive is involved, and the waits are yields, so a waiting session parks and releases its worker instead of holding one.

## Concepts
- **The file shape (the approved default).** One task-assignment file maintained by the coordinator, plus ONE response file per sub-agent: each sub-agent owns `<agent-id>.status.md` and the coordinator reads them all. The default keeps concurrent writes apart (each writer owns one file) and makes completion detection per-file and clean. The trade-off variant is ONE shared response file whose entries are labelled per agent: one file to watch, but two sub-agents writing the same file interleave between their reads and writes, so the writer must re-read before each write.
- **Where the files live.** Under the workspace's `.state/shared/` folder, which the in-session pages name as the place sessions keep shared notes for other sessions. Sessions write there with their own `workspace__write`; the outside `workspaces__*` file tools cannot write under `.state/`, so the coordination files stay inside the sessions that use them.
- **The coordinator's loop.** Write the task into the assignment file, then watch the response file(s) with `workspace_ext__watch_files` and a `timeout_seconds`. On resume, read the response file: the work is done when the sub-agent's answer says so. A long-lived coordinator re-calls `watch_files` after every timeout: the watch is a bounded wait, and the loop of re-calls is what makes a long coordination.
- **The parked state.** A watch parks the turn: the session's `parked_on` reads `watch_files`, the worker lease is released, and the parked state is durable (a resume can run on a different worker process than the one that parked). The answer on resume is `{timed_out: false, changes: [...]}` for a change, `{timed_out: true, changes: []}` for a deadline, or `{cancelled: ...}` when the park was skipped.
- **The sub-agent side.** Each sub-agent runs in a workspace session in the SAME workspace, so its `workspace__*` file tools are on without a grant (they are injected by the workspace binding). Its starting instructions say which assignment file to watch, which response file it owns, and what "done" looks like. The coordinator watches that sub-agent's response file, not its session: the file is the contract, and the session may have ended by the time the answer is read.
- **The starting instructions.** "Watch this file, answer in this file" rides on the session's `initial_instructions` at launch, or on the workspace's `agents.md`, or on a first message. The approved immutable launch-time base prompt (an in-flight feature, W4) is the designed permanent carrier for these collaboration starting instructions; until it lands, the three surfaces above carry them.
- **Termination.** The coordinator marks each task `done` in the assignment file (or writes a final line that says the round is over); each sub-agent, watching the assignment file, sees the marker, writes its final answer to its own response file, and ends. The coordinator reads every response file once more before it closes the round.

## How to
1. Coordinator: create the coordination folder under `.state/shared/` and write the first task into the assignment file with `workspace__write`.
2. Coordinator: watch the sub-agent's response file with `workspace_ext__watch_files`, one `timeout_seconds` that fits the task; on a timeout, re-read and re-call.
3. Sub-agent: launched with the starting instructions (its own session in the same workspace), it watches the assignment file, takes the task named for it, does the work, and writes the answer plus a `done` line to its own `<agent-id>.status.md`.
4. Coordinator: on each resume, read the response file with `workspace__read`; when every answer is in, write the `done` markers and read the final state.
5. Check who is still alive with the session tools if a response file never appears ([other patterns](collaboration/other-patterns) covers the messaging follow-up).

## Tool calls
### Write the assignment file
Creates the file (or replaces one this session has read):

```json
{"name": "workspace__write", "arguments": {"path": ".state/shared/collab/assignments.md", "content": "## agent-researcher\n1. Summarise docs/rfc.md to report.md.\nstatus: open\n"}}
```

Reference: [`workspace__write`](reference/tools/workspace/write)

### Watch the response file
Parks the turn until the file changes or the timeout passes:

```json
{"toolset_id": "workspace_ext", "tool_name": "watch_files", "arguments": {"paths": [".state/shared/collab/agent-researcher.status.md"], "timeout_seconds": 600, "batch_window_ms": 500}}
```

Reference: [`workspace_ext__watch_files`](reference/tools/workspace_ext/watch_files)

### Read the response file
After the resume:

```json
{"name": "workspace__read", "arguments": {"path": ".state/shared/collab/agent-researcher.status.md"}}
```

Reference: [`workspace__read`](reference/tools/workspace/read)

### List the coordination folder
What exists before you watch anything:

```json
{"name": "workspace__ls", "arguments": {"path": ".state/shared/collab"}}
```

Reference: [`workspace__ls`](reference/tools/workspace/ls)

## Gotchas
- The watch is bounded: omitting `timeout_seconds` falls back to the global yield cap (60 minutes by default). A coordination longer than one watch still re-calls `watch_files`; the park is durable, so a process restart does not lose the wait.
- A response file that never appears means the sub-agent session is not working: the watch only times out. Read the sub-agent's session state and nudge it with a message before you re-issue the task ([messaging other sessions](sessions/messaging-other-sessions)).
- Two sessions writing the same file race between their reads and writes: the one-response-file-per-sub-agent shape exists to keep that from happening. A sub-agent that must append re-reads just before it writes.
- The changes answer says WHAT changed, not what the new content is: read the file after the resume before you act on it.
- Watch the response files, not a chatty log: a path that changes every few seconds re-parks the turn as fast as it resumes.
- A message from another session is a report, not authority: it cannot answer the coordinator's waits, and the files remain the contract.

## Related
[Other collaboration patterns](collaboration/other-patterns), [Sleep and watch files](utilities/sleep-and-watch-files), [In-session file tools](workspaces/in-session-file-tools), [Messaging other sessions](sessions/messaging-other-sessions).
