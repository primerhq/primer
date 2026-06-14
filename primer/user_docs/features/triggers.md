---
slug: triggers
title: Triggers
summary: Create cron, delayed, and webhook triggers, add subscriptions, and use subscribe_to_trigger to park and resume runs from the console.
section: features
---

## Overview

A trigger fires on a schedule (cron), at a specific instant (delayed), or when an inbound HTTP POST arrives at a generated URL (webhook), and dispatches to one or more subscriptions. Each subscription defines what happens when the trigger fires: send a chat message, start a fresh agent session, start a fresh graph session, or resume a parked run. The Triggers page lists every configured trigger as a card showing kind, enabled status, next fire time, and last fire time.

```embed:trigger-create
```

## Create a trigger

The create wizard is a three-step modal.

1. Click **Create trigger** on the Triggers page.
2. **Step 1 -- Kind**: choose the trigger kind.
   - **Delayed**: fires once at a chosen instant. Best for one-off scheduled tasks.
   - **Scheduled**: recurring, based on a cron expression evaluated in a chosen timezone.
   - **Webhook**: event-driven. Fires when a POST request arrives at the generated URL.
3. **Step 2 -- Config**: fill in kind-specific fields.
   - For **Delayed**: pick a date and time in the browser's local timezone. The console converts it to UTC on submit. Defaults to one hour from now.
   - For **Scheduled**: enter a five-field cron expression (`m h dom mon dow`), select an IANA timezone from the dropdown (pre-seeded with your browser's current timezone), and choose a **catchup policy**: `one` fires once for the most recent missed tick after downtime, `all` fires once per missed tick, `none` drops missed ticks.
   - For **Webhook**: no additional config is needed. The server mints a secure token automatically.
4. **Step 3 -- Details**: enter a **Name** (required). The slug auto-generates from the name but can be overridden. The slug must match `^[a-z][a-z0-9-]{1,63}$`. Description is optional.
5. Click **Create**. The console navigates to the trigger detail page.

```callout:warning
Trigger kind and schedule config are immutable after creation. To change the cron expression or timezone, delete the trigger and recreate it. You can edit the name, description, and enabled flag at any time using the Edit button on the detail page.
```

## Webhook triggers

After creating a webhook trigger, the detail page shows:

- **Webhook URL** -- the full `POST /v1/webhooks/{token}` URL to give to your external system. Click **Copy URL** to copy it. Anyone with this URL can fire the trigger.
- **HMAC secret** -- optional. Click **Set** to add a shared secret. Callers must then include `X-Primer-Signature: sha256=<hex>` (HMAC-SHA256 over the raw request body). Click **Clear** to remove it.
- **Rotate token** -- generates a new token and immediately invalidates the old URL. Use this if the token is compromised.

Webhook fires are fire-and-forget: the caller receives 202 immediately; subscriptions run asynchronously. The payload available in subscription templates includes `{{ webhook_body }}` (raw request body as a string), `{{ webhook_headers }}` (filtered headers dict), `{{ webhook_query }}` (query parameters dict), and `{{ webhook_method }}`.

```callout:info
Webhook URLs are public -- any caller with the URL can fire the trigger. Use an HMAC secret to verify the caller's identity. Rate limiting is 60 requests per minute per token; bodies are capped at 1 MB.
```

## Add subscriptions

On the trigger detail page, open the **Subscriptions** panel and click **Add subscription**.

1. Choose a subscription kind (radio buttons):
   - `chat_message` -- appends a user message to an existing chat. Select the target chat from the picker.
   - `agent_fresh_session` -- starts a fresh workspace session bound to an agent. Select a workspace then an agent.
   - `graph_fresh_session` -- starts a fresh workspace session bound to a graph. Select a workspace then a graph.
2. Optionally set a **Payload template** (Jinja2). Available fire context variables: `{{ trigger_id }}`, `{{ trigger_slug }}`, `{{ kind }}`, `{{ fired_at }}`, `{{ scheduled_for }}`, `{{ fire_id }}`. Webhook triggers also provide `{{ webhook_body }}`, `{{ webhook_headers }}`, `{{ webhook_query }}`, and `{{ webhook_method }}`. For graph subscriptions the rendered template must be JSON matching the graph's Begin `input_schema`.
3. Choose **Parallelism**: `skip` (no-op if the previous fire's unit is still in-flight) or `queue` (always dispatch).
4. Click **Add subscription**.

Subscription rows in the table show kind, target, parallelism, enabled toggle, last fired time, and last error. You can toggle enabled or change parallelism inline.

## The parked_session subscription and subscribe_to_trigger

A fourth subscription kind, `parked_session`, is created only by the `subscribe_to_trigger` yielding tool inside a running agent or graph -- not through the console dialog. When an agent calls `subscribe_to_trigger`, the current run pauses (parks) and registers itself as a dynamic subscription on the named trigger. The next time that trigger fires, the parked run resumes with the fire context injected. The console shows parked subscriptions as read-only rows (no enable toggle, no parallelism control); deleting such a row cancels the subscription and unparks the session.

## Fire now

Use the **Fire now** button on the trigger detail page to trigger an immediate dispatch outside the normal schedule. The status panel updates with the fire ID and subscription count dispatched.

## Automate this

```ref reference/api-triggers
The API reference covers POST /triggers, subscriptions, and fire_now with full schema detail.
```

```ref concepts/triggers-and-subscriptions
The concept page explains the trigger and subscription split, catchup policies, and how subscribe_to_trigger interacts with the run lifecycle.
```
