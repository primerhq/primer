---
title: "Triggers"
summary: "Create, read, change, enable and delete triggers: delayed, scheduled, webhook and channel."
order: 1
tools: [crud__create_trigger, crud__update_trigger, trigger__list, trigger__get, trigger__create, trigger__update, trigger__delete]
related: [triggers/subscriptions, triggers/fire-now, channels/bindings]
---
## What it is
A trigger is a source of fires: a time, a schedule, a webhook call or a channel message. A trigger does nothing on its own. Its subscriptions say what each fire does ([subscriptions](triggers/subscriptions)). Manage triggers with `trigger__list`, `trigger__get`, `trigger__create`, `trigger__update` and `trigger__delete`. `crud__create_trigger` and `crud__update_trigger` are twins of create and update: same arguments, same code.

## Concepts
- **The four kinds.** `config.kind` picks one. The kind never changes after create.
  - `delayed` fires once at `fire_at`, an ISO 8601 instant (write it in UTC, ending in `Z`; a time without a zone is read as UTC). After that fire the trigger is disabled.
  - `scheduled` fires on a five-field `cron`, read in `timezone` (an IANA name, default `UTC`). `catchup` (`one`, `all` or `none`, default `one`) decides what happens after downtime (see Gotchas).
  - `webhook` fires when a caller sends `POST /v1/webhooks/{token}`. The server mints `token` (32 hex characters) on create, whatever you send. Optional: `hmac_secret` (the caller must sign the body), `interactive` (hold the POST until the fired runs end, default `false`) and `wait_timeout_seconds` (1 to 600, default 60: how long an interactive POST waits). See [fire a trigger now](triggers/fire-now).
  - `channel` fires when the inbound channel router receives an event from `provider_id`; set `channel_id` to listen to one channel only. `interactive` (default `true`) posts each run's result back into the thread. It fires from the router, never on a clock. Its subscriptions are channel bindings ([bindings](channels/bindings)).
- **Fields you set.** `slug` (required, unique, pattern `^[a-z][a-z0-9-]{1,63}$`, no `__`), `name` (required, at most 200 characters), `description` and `enabled` (default `true`).
- **Fields the server sets.** `id` (`tr-` and 12 hex digits), `next_fire_at` (the next fire of a `delayed` or `scheduled` trigger; null when disabled, after a `delayed` fire, and always for `webhook` and `channel`), `last_fired_at`, `last_fire_error` (the first failed delivery of the latest fire, as JSON; a clean fire clears it) and `owner`.
- **Ids, not slugs.** Every tool that asks for a trigger takes its `id`, such as `tr-3f9a1c0b7d42`. A slug answers `trigger_not_found`.
- **No payload here.** A trigger has no `payload_template`. Each subscription renders its own message from the fire.
- **Owner and rank.** Whoever last saved a trigger (created or updated it) is its `owner`: your run's principal when you call these tools, or you yourself when you call them straight over MCP. A fresh session a fire starts runs as the subscription's owner, at no more than the lower of the two owners' current roles. A trigger saved before owners were recorded fires as an ordinary user.
- **Who may change or delete.** A trigger is owned and shared like an agent ([sharing and access](start-here/sharing-and-access)). Changing it needs `full` access; deleting it, rotating a webhook token and changing its sharing need `owner`. A trigger you cannot read answers `trigger_not_found`, and too low a level is refused (403 `access_denied` over REST, `type=forbidden` from a tool) with nothing changed. Firing it by hand needs `full` too ([fire a trigger now](triggers/fire-now)), and so does adding or removing a subscription.
- **Webhook secrets.** Only the owner or an admin reads a webhook trigger's `token`; everyone else reads a masked value. `hmac_secret` always reads `**********`. Only the owner or an admin may update a webhook trigger (`forbidden` otherwise).
- **Approval.** `trigger__create`, `trigger__update`, `trigger__delete`, `crud__create_trigger` and `crud__update_trigger` are approval-gated by default. In a session the call parks until a person decides ([roles and approvals](start-here/roles-and-approvals)). The subscription tools are not gated.
- **Reach.** `trigger__list` and `trigger__get` show every trigger: a trigger is in no workspace. From a session outside the default workspace (prime), `trigger__update`, `trigger__delete` and `trigger__fire_now` answer `trigger_not_found`, as an unknown id does, when any subscription of the trigger starts or feeds a session outside your own workspace.
- Every tool here needs the `user` role. None needs an admin.

## How to
1. Pick the kind: one time (`delayed`), a schedule (`scheduled`), an outside system calling in (`webhook`) or a channel message (`channel`).
2. Check the slug is free with `trigger__list`.
3. Create the trigger with `trigger__create`. Keep the `id` it returns.
4. Add a subscription that says what a fire does ([subscriptions](triggers/subscriptions)). A trigger with no enabled subscription fires and does nothing.
5. Test it ([fire a trigger now](triggers/fire-now)), then read `last_fired_at` and `last_fire_error` with `trigger__get`.
6. Pause it with `trigger__update` and `enabled: false`. Remove it with `trigger__delete`.

## Tool calls
### List triggers
`trigger__list` returns every trigger, filtered by `kind` (any of the four) and `enabled` when you send them.

```json
{"toolset_id": "trigger", "tool_name": "list", "arguments": {"kind": "scheduled", "enabled": true}}
```

Reference: [`trigger__list`](reference/tools/trigger/list)

### Read one trigger
`trigger__get` returns the whole row: `next_fire_at`, `last_fired_at` and `last_fire_error` included.

```json
{"toolset_id": "trigger", "tool_name": "get", "arguments": {"id": "tr-3f9a1c0b7d42"}}
```

Reference: [`trigger__get`](reference/tools/trigger/get)

### Create a scheduled trigger
`trigger__create` with a `scheduled` config fires every day at 06:00 in Dubai.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "morning-digest", "name": "Morning digest", "config": {"kind": "scheduled", "cron": "0 6 * * *", "timezone": "Asia/Dubai", "catchup": "one"}}}
```

Reference: [`trigger__create`](reference/tools/trigger/create)

### Create a delayed trigger
A `delayed` trigger fires once, at `fire_at`.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "release-reminder", "name": "Release reminder", "config": {"kind": "delayed", "fire_at": "2026-11-02T09:00:00Z"}}}
```

### Create a signed webhook trigger
A `webhook` trigger gets its `token` from the server; the answer shows it to you.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "github-push", "name": "GitHub push", "config": {"kind": "webhook", "hmac_secret": "REPLACE-WITH-A-LONG-RANDOM-SECRET"}}}
```

### Create a channel trigger
A `channel` trigger listens to one channel of a channel provider.

```json
{"toolset_id": "trigger", "tool_name": "create", "arguments": {"slug": "oncall-room", "name": "On-call room", "config": {"kind": "channel", "provider_id": "cp-slack", "channel_id": "ch-slack-oncall"}}}
```

### Disable a trigger
`trigger__update` changes only the fields you send. A disabled trigger does not fire and keeps its subscriptions.

```json
{"toolset_id": "trigger", "tool_name": "update", "arguments": {"id": "tr-3f9a1c0b7d42", "enabled": false}}
```

Reference: [`trigger__update`](reference/tools/trigger/update)

### Change the schedule
Send the whole `config`: it replaces the old one.

```json
{"toolset_id": "trigger", "tool_name": "update", "arguments": {"id": "tr-3f9a1c0b7d42", "config": {"kind": "scheduled", "cron": "0 6 * * 1-5", "timezone": "Asia/Dubai", "catchup": "one"}}}
```

### Delete a trigger
`trigger__delete` removes the trigger and every subscription of it, and answers `{ok: true}`.

```json
{"toolset_id": "trigger", "tool_name": "delete", "arguments": {"id": "tr-3f9a1c0b7d42"}}
```

Reference: [`trigger__delete`](reference/tools/trigger/delete)

### Create through the crud twin
`crud__create_trigger` takes the same arguments as `trigger__create`.

```json
{"toolset_id": "crud", "tool_name": "create_trigger", "arguments": {"slug": "nightly-digest", "name": "Nightly digest", "config": {"kind": "scheduled", "cron": "0 2 * * *"}}}
```

Reference: [`crud__create_trigger`](reference/tools/crud/create_trigger)

### Update through the crud twin
`crud__update_trigger` takes the same arguments as `trigger__update`.

```json
{"toolset_id": "crud", "tool_name": "update_trigger", "arguments": {"id": "tr-3f9a1c0b7d42", "enabled": true}}
```

Reference: [`crud__update_trigger`](reference/tools/crud/update_trigger)

## Gotchas
- An update cannot change `config.kind` (`trigger_kind_immutable`). Delete the trigger and create a new one.
- An update that sends `config` replaces all of it. An omitted `timezone` falls back to `UTC` and `catchup` to `one`. An omitted `hmac_secret` clears the secret: send the `**********` a read shows to keep it. The webhook `token` is kept unless you send a new one.
- A delete also removes a session's wait on the trigger. That session stays parked until its wait times out.
- A `delayed` trigger fires once. Enabling it again does not fire it again: create a new one.
- A taken slug answers `trigger_slug_conflict`, and a slug outside the pattern `validation-error`. A bad cron or timezone answers `cron_invalid` or `timezone_invalid`.
- `catchup` decides what happens to the ticks a trigger missed (the server was down, or the fire came late): `none` skips them, `one` fires the most recent one, `all` fires each (at most 64, oldest first). Then the tick that is due fires once, in every policy. Each fire carries its own tick as `scheduled_for`. A trigger enabled again catches up nothing for the time it was disabled. Use `one` unless every tick must run.
- A fire is at least once. A worker that dies in the middle of a fire may deliver it again. A trigger is claimed like other work, so a busy deployment fires late.
- An accepted edit makes you the owner its runs act as, so it moves its runs to your rank (sharing is unchanged). When a save changes who a webhook trigger runs as, the server mints a new token and the old URL stops working.

## Related
[subscriptions](triggers/subscriptions), [fire a trigger now](triggers/fire-now), [waiting for events](triggers/waiting-for-events), [channel bindings](channels/bindings), [roles and approvals](start-here/roles-and-approvals).
