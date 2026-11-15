---
title: "Triggers"
summary: "Start work from schedules, webhooks and channel events, and pause a turn until an event happens."
order: 70
---

Triggers start work without a person: at a time, on a schedule, from a webhook or from a channel message. A subscription says what a fire does. This section also covers the tools that pause a turn until an event arrives. Start with [Triggers](triggers/triggers).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`event_subscriptions__create`](reference/tools/event_subscriptions/create) | Create EventSubscription. | admin |
| [`event_subscriptions__delete`](reference/tools/event_subscriptions/delete) | Delete EventSubscription. | admin |
| [`event_subscriptions__find`](reference/tools/event_subscriptions/find) | Find EventSubscription with predicate. | admin |
| [`event_subscriptions__get`](reference/tools/event_subscriptions/get) | Get EventSubscription by id. | admin |
| [`event_subscriptions__list`](reference/tools/event_subscriptions/list) | List EventSubscription. | admin |
| [`event_subscriptions__update`](reference/tools/event_subscriptions/update) | Replace EventSubscription. | admin |
| [`events__list_events`](reference/tools/events/list_events) | Read the platform event log. | user |
| [`events__set_subscription_paused`](reference/tools/events/set_subscription_paused) | Pause or resume an event subscription. | admin |
| [`triggers__get_access`](reference/tools/triggers/get_access) | Who Trigger is shared with. | user |
| [`triggers__put_access`](reference/tools/triggers/put_access) | Change who Trigger is shared with (owners only). | user |
| [`triggers__rotate_token`](reference/tools/triggers/rotate_token) | Rotate the webhook token for a webhook trigger. | user |
| [`webhooks__get_webhook_delivery`](reference/tools/webhooks/get_webhook_delivery) | Poll one webhook delivery for its result. | user |
| [`webhooks__receive_webhook`](reference/tools/webhooks/receive_webhook) | Receive an inbound webhook. | user |
