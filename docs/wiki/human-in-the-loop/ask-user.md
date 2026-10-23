---
title: "Ask the user"
summary: "Pause the turn and ask a person a question, with an optional answer schema and timeout."
order: 1
tools: [system__ask_user]
related: [human-in-the-loop/inform-user, channels/reply-binding, sessions/control-a-session]
---
## What it is
`system__ask_user` pauses your turn and asks a person a question. The turn holds no result until the operator answers, the park times out, or the park is cancelled.

## Concepts
- `prompt` carries the question, at most 8000 characters; newlines are preserved by the console panel.
- `response_schema` is an optional JSON Schema the answer must satisfy. The server checks the answer against it at POST time; a violation is shown to the operator inline and the turn stays parked. Omit it for a free-text answer.
- `timeout_seconds` is an optional per-call limit. Omitted, the park falls back to the global yield cap, 60 minutes by default.
- `files` are workspace-relative paths sent as media alongside the prompt.
- The result is `{response: ...}` on a reply, `{timed_out: true, elapsed_seconds: ...}` when the limit passes, or `{cancelled: true, reason: ...}` when the park ends without an answer.
- The question shows in the console and goes to the workspace's bound channel: [reply binding](channels/reply-binding).
- Only a person answers: a peer message is never the answer, and an agent cannot answer for the user.
- It is a core-kit tool, called directly by its scoped id. Reached through `system__call_tool`, the answer arrives as the `call_tool` result.
- In a graph, a `tool_call` node on `system__ask_user` parks the whole run until the answer.

## How to
1. Put every question that matters in one `prompt`; one ask per turn.
2. Add a `response_schema` when the answer must have a shape, a choice from a list or a field to fill; leave it out for free text.
3. Set `timeout_seconds` when the answer must not wait the full 60 minutes; otherwise omit it.
4. Call the tool; the turn parks while the operator reads the question.
5. Read the result: continue on `{response: ...}`, decide on a timeout whether to retry or proceed, and stop asking on a cancel.

## Tool calls
### Ask a question
Send the question and park the turn until the operator answers.

```json
{"name": "system__ask_user", "arguments": {"prompt": "Proceed with the deploy to production?"}}
```

Reference: [`system__ask_user`](reference/tools/system/ask_user)

## Gotchas
- Ask once, with the questions that matter; a chain of small parks reads as noise.
- A status update is `misc__inform_user`, not a question; a fixed wait is `workspace_ext__sleep`.
- While parked the turn holds no result, and the run releases its lease; the answer, the timeout or a cancel wakes it again.
- A run whose principal is not an admin may not use `pattern` or `patternProperties` in the `response_schema`; the server runs those as regular expressions on the answer and refuses the call with a `forbidden` error naming the paths.

## Related
[inform the user](human-in-the-loop/inform-user), [reply binding](channels/reply-binding), [control a session](sessions/control-a-session).
