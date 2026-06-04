---
slug: sessions
title: Sessions
section: features
summary: The sessions list, session detail, transcript inspection, retry, pause, resume, cancel.
---

## The sessions list

Sessions land in the list as soon as they exist. The filter row
at the top supports filtering by status (running, parked, done,
failed) and by agent. Hitting a row opens the session detail.

```mockup:sessions-list-empty
{ "emptyLine": "No sessions yet", "ctaLabel": "New session" }
```

The empty state shows on a fresh install. Once sessions exist the
list paginates, newest first.

## The session detail page

Clicking a session row opens the detail page. The header strip
shows the session id, the bound agent, and the current status.
The transcript pane streams turns as they land.

```mockup:session-detail-panel
{ "sessionId": "sess-a1b2c3", "agentId": "weekly-digest", "status": "running", "turnCount": 4 }
```

A parked session shows the parked reason in the footer:

```mockup:session-detail-panel
{ "sessionId": "sess-9z8y7x", "agentId": "release-bot", "status": "parked", "turnCount": 7, "parkedReason": "trigger:gh-pr-merged" }
```

The footer's parked-reason field is the literal event key the tool
yielded on. Use it to track down where the session is stuck.

## Creating a session

Three entry points produce identical results.

```code-tabs:python,curl
--- python
sess = client.sessions.create(
    agent_id="weekly-digest",
    input="Summarise yesterday.",
)
print(sess.id)
--- curl
curl -X POST https://primer.example/v1/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"agent_id":"weekly-digest","input":"Summarise yesterday."}'
```

The console New session button calls the same endpoint. So does a
trigger subscription with `target=start_session`.

## Pause, resume, cancel

Three operator controls:

- **Pause** holds the next turn at the boundary. The worker
  releases the slot; the session moves to `parked`. Resume
  reverses it.
- **Cancel** moves the session to `cancelled` immediately. Any
  in-flight tool call gets a cancellation error; the transcript
  is preserved.
- **Retry from turn N** rewinds the transcript to turn N and
  re-runs from there. Useful when a tool failure made the agent
  go off-track.

```ref:concepts/sessions
The concept page covers the turn loop and the parked state in
detail.
```
