---
slug: rest-api-channels-triggers
title: REST API - channels, triggers, approvals
section: reference
summary: Enumerated endpoints for the integrations + ops surface.
---

## Channel providers

| Method | Path | Body |
|---|---|---|
| GET | `/v1/channel_providers` | - |
| POST | `/v1/channel_providers` | `{kind, name, config}` |
| GET | `/v1/channel_providers/{id}` | - |
| PATCH | `/v1/channel_providers/{id}` | partial |
| DELETE | `/v1/channel_providers/{id}` | - |

## Channels

| Method | Path | Body |
|---|---|---|
| GET | `/v1/channels` | - |
| POST | `/v1/channels` | `{provider_id, channel_id, name}` |
| POST | `/v1/channels/{id}/associate` | `{agent_id, mode}` |

```code-tabs:python,curl
--- python
prov = client.channels.create_provider(
    kind="slack",
    name="ops-slack",
    config={"bot_token": "xoxb-..."},
)
chan = client.channels.create(
    provider_id=prov.id, channel_id="C012345", name="ops-alerts",
)
client.channels.associate(agent_id="release-bot", channel_id=chan.id)
--- curl
curl -X POST https://primer.example/v1/channel_providers \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"kind":"slack","name":"ops-slack","config":{"bot_token":"xoxb-..."}}'
```

## Triggers

| Method | Path | Body |
|---|---|---|
| GET | `/v1/triggers` | - |
| POST | `/v1/triggers` | `{name, kind, ...kind-specific, subscription_target, ...}` |
| GET | `/v1/triggers/{id}` | - |
| POST | `/v1/triggers/{id}/fire` | force-fire (operator-only) |
| POST | `/v1/triggers/{id}/webhook` | inbound (HMAC-signed) |

Trigger kinds and their kind-specific fields:

| Kind | Required fields |
|---|---|
| `cron` | `cron_expression` |
| `webhook` | (none; `webhook_secret` is auto-generated) |
| `channel-pattern` | `channel_id`, `pattern` |

## Tool approval

| Method | Path | Body |
|---|---|---|
| GET | `/v1/tool_approval/policies` | - |
| POST | `/v1/tool_approval/policies` | `{toolset_id, tool_name, kind, ...kind-specific}` |
| GET | `/v1/tool_approval/queue` | parked approval prompts |
| POST | `/v1/tool_approval/decide` | `{policy_call_id, decision}` |

```callout:info
The `decide` endpoint is what the console clicks behind the
scenes when an operator approves or rejects. Mint a token with
the `tool_approval:write` scope to drive it from a bot.
```
