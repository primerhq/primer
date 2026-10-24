---
title: "Inbound bindings"
summary: "Route channel messages to agents: a channel trigger plus a binding with a matcher, an action and a reply target."
order: 3
tools: [system__create_channel_binding, system__list_channel_bindings, system__delete_channel_binding]
related: [triggers/triggers, triggers/subscriptions, channels/reply-binding]
---
## What it is
An inbound binding routes messages from a room to work: it starts an agent or a graph session, or feeds a session that already runs. A binding is a [subscription](triggers/subscriptions) on a `channel` trigger, with an `event_matcher` (which messages), a `config` (the action) and a `reply_target`. Create one with `system__create_channel_binding`, read a trigger's bindings with `system__list_channel_bindings` and remove one with `system__delete_channel_binding`. They need the `user` role and are not approval-gated by default.

## Concepts
- **The route of one message.** The bot hears a message in a room that has a [channel](channels/channels) and turns it into a channel event. Then:
  1. A reply to an open question or approval answers it ([reply binding](channels/reply-binding)).
  2. A message in a conversation already mapped to a session goes to that session as a new message. A session in a turn gets it after the turn; an ended one reopens.
  3. Anything else fires every enabled `channel` trigger of the provider whose `channel_id` is unset or names this channel. Each enabled binding of those triggers tests its `event_matcher`, and each match runs its action.
- **The channel event.** `type`, `surface` (`dm`, `channel` or `thread`), `text`, `command` (`name` and `args`), `sender` (`external_id`, `display_name`, `roles`, `is_bot`), `mentions_bot`, `room_external_id` (the platform's room id) and `thread_anchor`. What each platform sends today:
  - Slack: `message.posted`, surface `channel` or `thread` (a Slack DM is `channel`). Slack slash commands are not received.
  - Discord: `message.posted`, surface `channel`, `thread` or `dm`. Discord slash commands are not received.
  - Telegram: `message.posted`, and `command.invoked` for a message that starts with `/name` (`command.name` is `name`, `command.args` the rest). Surface `dm` or `channel`.
  - The other types (`component.acted`, `reaction.added` and the rest) never reach a binding. No platform fills `mentions_bot` or `sender.roles` today.
- **`event_matcher`** holds when every field you set holds. `event_type` is required. The rest are optional: `surface` (a list), `room_external_ids` (platform room ids), `command_name`, `mentions_bot`, `sender_ids_any` (platform user ids), `sender_roles_any` and `text_pattern`. Leave `event_matcher` out to act on every event of the trigger.
- **`text_pattern`** is a regular expression searched anywhere in `text`, at most 1000 characters. Nested unbounded repeats such as `(a+)+`, comment groups, verbose mode and inline flags past the start are refused (`validation-error`). Each search may take 10 ms, and one event has 100 ms for every pattern, shared between the bindings' owners; a binding past its owner's share is skipped (`skipped_match_budget`).
- **`config`** is the action:
  - `agent_fresh_session` (`workspace_id`, `agent_id`) starts a new agent session.
  - `graph_fresh_session` (`workspace_id`, `graph_id`) starts a new graph session. Its rendered payload must be a JSON object.
  - `session_append` (`session_id`) sends the message into an existing session.
  - `parked_session` belongs to `workspace_ext__subscribe_to_channel_event` ([waiting for events](triggers/waiting-for-events)). Never send it.
- **`payload_template`** is the Jinja template of the session's message. A channel fire adds `event` (the channel event) to the fire context, so `{{ event.text }}` sends the text alone. With no template, the message is the whole fire context as JSON. See [subscriptions](triggers/subscriptions) for `parallelism`: a fresh-session binding never skips a channel event.
- **Conversations.** The first session a fire starts or feeds is mapped to the message's conversation: its platform thread (Slack, Discord) or, for a DM, its sender (Telegram, Discord). Later messages there go to that session (step 2), and its questions and answers come back there. A message outside a thread or a DM (a room post, any Slack DM) maps nothing: each one fires again, and its session answers through its workspace's [reply binding](channels/reply-binding), if it has one.
- **How much a conversation hears** is the trigger's `interactive` flag (default `true`): each turn's answer is posted back. With `interactive: false` only the session's questions and approvals are.
- **Sessions a binding starts are autonomous.** Each ends when its turn finishes cleanly. A later message in its conversation reopens it.
- **`reply_target`** is stored on the binding: `source_thread` (the default), `source_room`, `dm_sender`, `none`, or `{channel_id, anchor, quiet}`. The server does not read it today: the answer goes where the two points above say.
- **Owner and rank.** Saving a binding makes you its owner. A session it starts runs as you, at no more than the lower of your role and the trigger owner's. A `session_append` fire into a session that runs above that rank fails (`steer_outranks_fire`). A caller who does not manage a binding (not its owner, not an admin) sees only the owner's name and role in the list.
- **Reach.** From a session outside the default workspace (prime), `config` may name only your own workspace or a session in it: another answers `not-found`, as an unknown id does, and every fire checks it again (`target_out_of_reach`). The list leaves out other bindings, and delete answers `subscription_not_found` for them. From prime, and with no calling session, every workspace is in reach.
- **The room is trusted.** Anyone in the room can make a binding act, and its session runs at the owner's rank. A reply in a mapped conversation is not ranked at all. Bind only rooms whose members may do that.

## How to
1. An admin registers the [provider](channels/providers). Add the [channel](channels/channels) with `chats.enabled: true`.
2. Create the `channel` trigger with `trigger__create` ([triggers](triggers/triggers)) and keep its `id` (`tr-` and 12 hex digits). The trigger is approval-gated by default.
3. Create the binding with `system__create_channel_binding`.
4. Post a matching message in the room. Read `last_fired_at` and `last_fire_error` on the trigger with `trigger__get`, and on the binding with `system__list_channel_bindings`.
5. Change `enabled`, `payload_template` or `parallelism` with `trigger__update_subscription`. To change the matcher or the action, delete the binding and create it again.

## Tool calls
### Run an agent on a Telegram command
`system__create_channel_binding` starts the `deployer` agent on `/deploy`, with the command's arguments in its message.

```json
{"toolset_id": "system", "tool_name": "create_channel_binding", "arguments": {"trigger_id": "tr-5c1e8a2b9d07", "event_matcher": {"event_type": "command.invoked", "command_name": "deploy"}, "config": {"kind": "agent_fresh_session", "workspace_id": "ws-ops", "agent_id": "deployer"}, "payload_template": "Deploy request: {{ event.command.args }}", "description": "/deploy runs the deployer"}}
```

Reference: [`system__create_channel_binding`](reference/tools/system/create_channel_binding)

### Answer questions asked in Slack threads
A thread message that ends with a question mark starts the `helpdesk` agent with the text alone.

```json
{"toolset_id": "system", "tool_name": "create_channel_binding", "arguments": {"trigger_id": "tr-7a4d2e9c1b36", "event_matcher": {"event_type": "message.posted", "surface": ["thread"], "text_pattern": "\\?\\s*$"}, "config": {"kind": "agent_fresh_session", "workspace_id": "ws-docs", "agent_id": "helpdesk"}, "payload_template": "{{ event.text }}"}}
```

### Start a graph for two people
A graph binding renders a JSON object as the graph's input.

```json
{"toolset_id": "system", "tool_name": "create_channel_binding", "arguments": {"trigger_id": "tr-5c1e8a2b9d07", "event_matcher": {"event_type": "message.posted", "sender_ids_any": ["111222333", "444555666"]}, "config": {"kind": "graph_fresh_session", "workspace_id": "ws-ops", "graph_id": "triage-and-route"}, "payload_template": "{\"report\": {{ event.text | tojson }}}"}}
```

### Feed a running session
`session_append` sends every DM to one session. `queue` keeps a message that arrives during a turn.

```json
{"toolset_id": "system", "tool_name": "create_channel_binding", "arguments": {"trigger_id": "tr-5c1e8a2b9d07", "event_matcher": {"event_type": "message.posted", "surface": ["dm"]}, "config": {"kind": "session_append", "session_id": "sess-3f9a1c8d2b7e"}, "payload_template": "{{ event.text }}", "parallelism": "queue"}}
```

### List a trigger's bindings
`system__list_channel_bindings` returns the bindings with their `last_fired_at` and `last_fire_error`.

```json
{"toolset_id": "system", "tool_name": "list_channel_bindings", "arguments": {"trigger_id": "tr-5c1e8a2b9d07"}}
```

Reference: [`system__list_channel_bindings`](reference/tools/system/list_channel_bindings)

### Delete a binding
`system__delete_channel_binding` removes one binding and answers `{ok: true}`. The trigger stays.

```json
{"toolset_id": "system", "tool_name": "delete_channel_binding", "arguments": {"trigger_id": "tr-5c1e8a2b9d07", "subscription_id": "sb-0d4e7a91c2f3"}}
```

Reference: [`system__delete_channel_binding`](reference/tools/system/delete_channel_binding)

## Gotchas
- An unknown trigger answers `trigger_not_found`. The tool does not check the trigger's kind: on a trigger that is not `channel`, a binding with a matcher never matches, and one without acts on every fire.
- `trigger__fire_now` on a channel trigger carries no event: every binding with a matcher skips (`skipped_no_match`). Test with a real message.
- The trigger's `channel_id` is the channel's id (`ch-...`). `room_external_ids` holds the platform's ids.
- A template that reads a missing value fails the delivery (`payload_template_failed`): a plain message has no `event.command`. Match `command.invoked` before you read `command`.
- A matcher on `mentions_bot: true` or on `sender_roles_any` never matches today.
- In a Slack or Discord thread with an open question, the next reply is its answer, whatever it says. The replies after it go to the session as new messages, and the thread keeps its session. A reply while an approval waits is a new message too, not a decision: use the buttons.
- `parallelism` must be `skip` or `queue`. The tool accepts any string, and another value fails the call.

## Related
[triggers](triggers/triggers), [subscriptions](triggers/subscriptions), [waiting for events](triggers/waiting-for-events), [channels](channels/channels), [reply binding](channels/reply-binding).
