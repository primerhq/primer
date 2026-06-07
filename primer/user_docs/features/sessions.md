---
slug: sessions
title: Sessions
section: features
summary: Start, observe, and control a session in the console -- start a run, watch turns stream in, and pause, resume, or cancel.
---

## Overview

A session is a single run of an agent. It owns the full conversation transcript, the workspace slot, and the worker claim for each active turn. The Sessions page lists all sessions, newest first, with live status updates every three seconds.

```embed:sessions-list
```

## Starting a session

1. Navigate to Sessions in the left nav.
2. Click **New session** (top-right of the filter bar).
3. Select the agent to run and provide the initial input text.
4. Click **Start**. The new session row appears at the top of the list with status `created`, then transitions to `running` as a worker picks it up.

You can also start a session from an agent's detail page using the **Chat** button, which opens an interactive chat session without requiring a workspace.

## Filtering and sorting the list

The filter bar supports:

- **Status chips**: click one or more of created / running / waiting / paused / ended / failed / cancelled to filter by status. Active-status chips (running, paused) are highlighted.
- **Agent** dropdown: narrows to sessions bound to a specific agent.
- **Workspace** dropdown: narrows to sessions running in a specific workspace.
- **Text search**: matches against session ID, agent ID, graph ID, or workspace ID.
- **Column headers**: click to sort by created time, last-turn time, agent, or worker. Click again to reverse direction.

## Viewing session detail

Click any row to open the session detail view.

```embed:session-detail
```

The detail view shows:

- **Header strip**: session ID, bound agent, current status, and elapsed time.
- **Transcript pane**: turns stream in as they land. Each turn shows the role (user/assistant/tool), content, and timestamp.
- **Footer**: for sessions in `waiting` or `paused` state, the footer shows the reason the session stopped -- typically the event key the agent yielded on. Use this to diagnose where the session is blocked.

## Pausing, resuming, and cancelling

Three operator controls appear in the session detail header:

- **Pause**: holds the session at the next turn boundary. The worker releases the slot. The session moves to `paused`. Use this to inspect state before the next turn runs.
- **Resume**: reverses a pause. The session re-enters the queue and a worker picks it up at the next turn.
- **Cancel**: moves the session to `cancelled` immediately. Any in-flight tool call receives a cancellation error. The transcript is preserved and readable after cancellation.

```callout:warning
Cancel is immediate and irreversible. If the agent was mid-write (writing a file, sending a message), the write may or may not have completed before the cancellation error arrived. Check the transcript to see where the last tool call landed before assuming the operation is rolled back.
```

## Retrying from a specific turn

On the session detail, use the retry control to rewind the transcript to a specific turn and re-run from that point. This is useful when a tool failure caused the agent to go off-track and you want to re-run without starting a new session from scratch.

## Automate this

```ref:reference/api-sessions
Full session resource schema, list/create/control endpoints, and transcript inspection.
```

## See also

```ref:concepts/sessions
The concept page explains the turn loop, the parked state, worker claims, and how sessions relate to workspaces.
```
