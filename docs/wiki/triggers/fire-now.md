---
title: "Fire a trigger now"
summary: "Fire a trigger on demand to test it, and how webhook triggers are called from outside."
order: 3
tools: [trigger__fire_now]
related: [triggers/triggers, triggers/subscriptions]
---
## What it is
`trigger__fire_now` fires a trigger at once, outside its schedule. It runs the normal dispatch while you wait, so you can test a trigger and its subscriptions. A webhook trigger is also fired from outside, by an HTTP call to its URL.

## Concepts
- **The answer.** `{skipped, fire_id, results}`. The call returns once every enabled subscription was dispatched; a session it started runs on its own afterwards. Each result has `subscription_id`, `ok`, `skipped`, `error_code`, `error_message` and `artefact_id` (the session it started or fed). A disabled trigger answers `{skipped: true, fire_id: null, results: []}` and fires nothing.
- **It is a real fire.** It writes the trigger's `last_fired_at` and `last_fire_error` and each subscription's outcome, and records a `trigger.fired` event. Its `fire_id` comes from the clock, so two calls fire twice.
- **Every kind is delivered.** `trigger__fire_now` dispatches exactly as Fire now on the trigger in the console (`POST /v1/triggers/{id}/fire_now`) does: it starts agent and graph sessions, steers sessions and wakes a session that waits on the trigger.
- **Webhook calls.** An outside system sends `POST /v1/webhooks/{token}` with any body up to 1 MB. There is no login: the token in the URL is the credential. Each token takes 60 calls a minute.
  - A trigger that is not `interactive` answers `202` with `{delivery_id, status: "accepted"}`, and the fire runs in the background.
  - An `interactive` trigger holds the call until the fired runs end and answers `200` with `{fire_id, results}`. Past `wait_timeout_seconds` it answers `202` with `{delivery_id, status: "pending"}`; the caller then polls `GET /v1/webhooks/{token}/deliveries/{delivery_id}` for `status`, `fire_id` and `results`.
  - Refusals: `404` for an unknown token, `403` for a disabled trigger, `401` for a bad signature, `413` for a body over 1 MB, `429` for too many calls.
- **The signature.** With an `hmac_secret` set, the caller must send `X-Primer-Signature: sha256=<hex>`: the lowercase hex HMAC-SHA256 of the raw body, keyed with the secret. A bare hex digest is accepted too. A missing or wrong signature is refused with `401`, and nothing fires.
- **What the run sees.** A webhook fire adds `webhook_body` (the raw body as text), `webhook_headers` (without `Authorization`, cookies, `X-Primer-Signature` and the forwarding headers), `webhook_query` and `webhook_method` to the fire context ([subscriptions](triggers/subscriptions)).
- **Rotating the token.** A person rotates it with `POST /v1/triggers/{id}/rotate_token` (the console's rotate control); only the owner or an admin may. The old URL stops working at once. No tool rotates a token.
- **Delivery.** Every POST is its own fire: a sender that retries fires the trigger again. A delivery cut short by a crash after its `202` runs again when the server restarts.

## How to
1. Create the trigger and its subscriptions ([triggers](triggers/triggers), [subscriptions](triggers/subscriptions)).
2. Fire it with `trigger__fire_now` and the trigger's `id`.
3. Read each result: `ok`, `skipped` and `error_code` say what the subscription did, and `artefact_id` names the session. Follow that session ([observe a session](sessions/observe-a-session)).
4. For a webhook, give the caller the URL `https://<your host>/v1/webhooks/<token>` and the secret. As the owner you read the token in the create answer or with `trigger__get`.
5. The caller signs the exact bytes it sends. For example, with `curl` and `openssl`:

```bash
BODY='{"event": "order.placed", "order_id": "123"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
curl -X POST "https://primer.example.com/v1/webhooks/$TOKEN" -H "Content-Type: application/json" -H "X-Primer-Signature: sha256=$SIG" -d "$BODY"
```

## Tool calls
### Fire a trigger now
`trigger__fire_now` runs the dispatch and answers one result per enabled subscription.

```json
{"toolset_id": "trigger", "tool_name": "fire_now", "arguments": {"id": "tr-3f9a1c0b7d42"}}
```

Reference: [`trigger__fire_now`](reference/tools/trigger/fire_now)

## Gotchas
- Use the trigger's `id`, not its slug.
- A fire now skips the clock, not `parallelism`: a `skip` subscription whose last session is still at work starts nothing (`skipped_subscription_busy`).
- On a webhook trigger, `trigger__fire_now` has no request: `webhook_body`, `webhook_headers` and `webhook_query` are missing, and a template that names them fails with `payload_template_failed`.
- On a channel trigger there is no event: every binding with an `event_matcher` skips (`skipped_no_match`).
- From a session outside the default workspace (prime), a trigger with a subscription outside your own workspace answers `trigger_not_found`, as an unknown id does.
- A webhook fire is at least once. If the sender may retry, make the run safe to repeat.

## Related
[triggers](triggers/triggers), [subscriptions](triggers/subscriptions), [observe a session](sessions/observe-a-session).
