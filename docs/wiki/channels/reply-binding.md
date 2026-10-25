---
title: "Reply binding"
summary: "Send a workspace's questions, approvals and updates to a channel by binding the workspace to it."
order: 4
tools: [system__set_reply_binding, system__clear_reply_binding]
related: [channels/channels, human-in-the-loop/ask-user, approvals/policies]
---
## What it is
A workspace's reply binding is the [channel](channels/channels) its sessions use to reach people. The workspace row's `reply_binding` names one channel. Set it with `system__set_reply_binding` and remove it with `system__clear_reply_binding`. Both need the `user` role and are not approval-gated by default.

## Concepts
- **What is posted.** From every session in the workspace: each `system__ask_user` question, each approval request of a gated tool call, each `misc__inform_user` message, and the answer that ends each turn. A run stopped by its tool-turn cap posts a notice that says so. There is no start message: a session that never asks or answers posts nothing.
- **Where it lands.** On Slack and Discord each session gets its own thread, opened by its first post (`Agent session <id>`). On Telegram the posts go straight into the room. With an `anchor`, every post goes into that thread instead.
- **The header.** A question or an approval request starts with `Workspace: <name> · Session: <label>`, so the room knows which work is asking.
- **The session's own binding wins.** A session that a channel conversation started or fed ([inbound bindings](channels/bindings)) is bound to that conversation, and posts there instead. A session with neither binding posts nowhere.
- **Answering.** On Slack and Discord, reply in the session's thread. On Telegram, reply to the question's message. An approval request has Approve and Reject buttons, and Reject asks for a reason. The first answer wins, from the console or the room.
- **Who may answer.** Anyone in the room. Channel answers and decisions are not ranked, by design: a person chose to bind the room. A gate whose policy names its approvers (specific users, roles, or admins only) is the exception. A platform user is not a primer user, so such a gate cannot be decided from the room: the clicker is told to decide it in the console. The gates primer ships on admin-only writes are of this kind. See [approval policies](approvals/policies).
- **What an approval request shows.** Only the arguments the gate's preview allows (the policy's `preview_args`, else a default rule). Every other value reads `<hidden>`. The decision still acts on the real arguments.
- **One binding per workspace.** Setting it again replaces it. Read it with `workspaces__get_workspace`: `reply_binding` is `{channel_id, anchor}`, or `null`.
- **`anchor`** names one thread in the channel: a Slack thread ts or a Discord thread id. Every approval request, update and answer of every session in the workspace goes into it, except an `system__ask_user` question, which opens the session's own thread in that channel with its own mapping: several sessions share the anchor's thread, and a question posted there would leave the thread mapped to the last asker. Telegram has no threads and ignores it. On Discord, an anchor that is not a thread id is ignored too.
- **Reach.** From a session outside the default workspace (prime), both tools reach only your own workspace. Another workspace answers `not-found`, exactly as an unknown one, whatever channel you name. From prime, and with no calling session, every workspace is in reach.

## How to
1. Find the channel's `id` with `system__find_channels` or `system__list_channels`.
2. Bind the workspace with `system__set_reply_binding`.
3. The next question, approval request, update or answer from a session in the workspace posts to the channel. The bot starts listening in that room with its first post.
4. Detach the workspace with `system__clear_reply_binding`. Sessions then reach people in the console only.

## Tool calls
### Bind a workspace to a channel
`system__set_reply_binding` stores the binding on the workspace and answers `{ok, workspace_id, channel_id, anchor}`.

```json
{"toolset_id": "system", "tool_name": "set_reply_binding", "arguments": {"workspace_id": "ws-ops", "channel_id": "ch-slack-oncall"}}
```

Reference: [`system__set_reply_binding`](reference/tools/system/set_reply_binding)

### Clear the binding
`system__clear_reply_binding` removes it and answers `{ok, workspace_id}`. It is safe when nothing is bound.

```json
{"toolset_id": "system", "tool_name": "clear_reply_binding", "arguments": {"workspace_id": "ws-ops"}}
```

Reference: [`system__clear_reply_binding`](reference/tools/system/clear_reply_binding)

## Gotchas
- An unknown workspace or channel answers `not-found`. Nothing else is checked: a room whose provider token is wrong stays silent.
- A failed post is only logged. The session stays parked on its question, and the room never sees it: answer it in the console.
- A reply that comes after the question timed out, or after someone answered it in the console, does nothing.
- On Telegram, a plain message in a DM that is mapped to the session is a new message, not the answer. It waits until the question is answered. Reply to the question's message instead.
- A Telegram approval button stops working after a restart, and says so: decide that request in the console.
- The binding covers every session in the workspace, graph and background sessions too. Bind a room that expects that traffic.
- Deleting the channel does not clear the binding, and posts then go nowhere. Clear it first.
- A plain reply typed in the anchor's thread steers nothing: no thread-to-session row names that thread, so it is an ordinary channel message for the channel's [inbound bindings](channels/bindings). The question's own thread (and a thread a session was started in) is the one that steers.

## Related
[channels](channels/channels), [inbound bindings](channels/bindings), [ask the user](human-in-the-loop/ask-user), [inform the user](human-in-the-loop/inform-user), [approval policies](approvals/policies).
