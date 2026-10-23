---
title: "Inform the user"
summary: "Send a person a one-way update and keep working."
order: 2
tools: [misc__inform_user]
related: [human-in-the-loop/ask-user, channels/reply-binding]
---
## What it is
`misc__inform_user` sends the operator a one-way message and keeps working. It waits for nothing: the turn continues immediately, and the result is `{delivered_to: <int>}`.

## Concepts
- `message` carries the update, at most 8000 characters; newlines are preserved.
- `files` are optional workspace-relative paths, attached to the message as media.
- The message reaches the console session and the workspace's bound channel: [reply binding](channels/reply-binding) decides the channel. It is a delivery, not a conversation.
- It is a core-kit tool, called directly by its scoped id.
- Use it for progress notes, for saying a long task finished, and for saying the run is blocked. A question is `system__ask_user`.

## How to
1. Send the update when the operator would otherwise not know where the task stands: a long step starts, a step ends, the run is blocked.
2. Keep it to the fact: what happened, what is next.
3. Read the result as a delivery count, not a reply, and keep the turn running.

## Tool calls
### Send an update
Send a one-way status message and keep the turn running.

```json
{"name": "misc__inform_user", "arguments": {"message": "Migration finished; 42 rows moved, nothing to approve."}}
```

Reference: [`misc__inform_user`](reference/tools/misc/inform_user)

## Gotchas
- It never asks: the tool waits for nothing, so a question sent this way gets no answer.
- A decision that blocks the run is `system__ask_user`; the inform is the note before or after it.

## Related
[ask the user](human-in-the-loop/ask-user), [reply binding](channels/reply-binding).
