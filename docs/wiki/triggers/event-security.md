---
title: "Event waits, canvas input and experimental tool batches"
summary: "Keep event filters within the caller's workspace and distinguish ordinary yields from experimental tool_wait batches."
order: 5
related: [triggers/waiting-for-events, human-in-the-loop/canvas, sessions/observe-a-session]
---

## What it is

A wait returns event data into an agent's context, so its filters and result are an authorization boundary. A lifecycle event is not permission to read another person's private input, and an experimental tool batch is not a question or an approval to answer.

## Concepts

- A non-admin run may wait on the allowed lifecycle kinds: `session.*`, `workspace.*`, `canvas.*`, `tool.called`, `turn.started`, `trigger.fired` and `llm.called`. Other kinds and a glob broad enough to include them are refused before a wait is stored.
- Such a run may not supply `expr`; its field operations are `eq` and `prefix`, not `regex`. A named workspace must be readable; an omitted workspace constraint pins delivery to the run's own workspace. Revoking that grant stops delivery while it is parked.
- A non-admin canvas wait must constrain its own workspace or session with an eq matcher. Canvas event-log envelopes omit entered `value` and Html `data`, even for an admin's wait and for your own session's event. Read the actual input through the session's canvas_get or canvas_wait; an event already dropped from the canvas event list may no longer carry it, while current values remain.
- A non-admin filter cannot infer hidden canvas input by matching the whole payload, value, data or descendants. The console's readable session canvas can still show that session's input; event-log redaction does not change workspace read permissions.
- Admin event filters may use bounded regex and data-only Rego. No principal may reference the host `opa` package or Rego template strings. Regex patterns are capped at 1000 characters and each match at 10 ms; an expression taking more than half a second is a non-match, and concurrent expression evaluation is bounded. Refusal is not a reason to retry the same unsafe expression.
- Returned event payloads scrub token, key, password, header and environment secrets. Broad key-name matching can also mask non-secret event_key or idempotency_key fields. Filters match the underlying data; the returned envelope is still scrubbed, and cannot be used to recover a secret.

## How to

1. Select the narrow kinds and readable workspace the task needs. Prefer eq or prefix filters rather than a broad glob.
2. Wait through the existing event tool and retain its actual result. For a canvas input, use the session-scoped canvas surface rather than guessing from a scrubbed event.
3. A person answers ask_user through its pending-question surface. Non-admin response schemas may not contain `pattern` or `patternProperties`, including through definitions or references; use enum, const or length constraints instead.
4. On a forbidden filter, fix its scope or ask the operator. A wait that cannot be authorized is not a subscription that will eventually work.

## Tool calls

See the exact worked calls on [waiting for events](triggers/waiting-for-events) and [canvas](human-in-the-loop/canvas). Standing event-log sink subscriptions have separate [admin operations](triggers); they do not change these restrictions on a run's wait.

## Experimental tool batches

`tool_calls_as_claims_enabled` defaults off. A `yielded` record with `kind: "tool_wait"` carries outstanding_task_ids and notifying_task_ids for a batch; it is not an ordinary yielding tool, question or approval. Standalone non-yielding agent batches have an executor, but the overall flag-on path remains unfinished: graph binding and cancel fan-out are unsupported, nested topology is deliberately cut, and there is no flag-on live-stack acceptance. Human task gates remain disabled; ask-user, channel, cancel, timeout and reconciliation decision surfaces are not an enabled task-gate workflow. Report such an unsupported park to the operator rather than submitting an ordinary approval response. Do not enable this development switch to implement a recipe.

## Gotchas

- A parked session holds no worker, but a stored park alone is not proof it has a supported wake path.
- `canvas.updated` carries no entered input; do not substitute it for the actual canvas event or current values.
- A secret masked in an event must be obtained through the legitimate credential workflow, never inferred by changing filters.

## Related

[Waiting for events](triggers/waiting-for-events), [canvas](human-in-the-loop/canvas), [approval context](approvals/decision-context), [observe a session](sessions/observe-a-session).
