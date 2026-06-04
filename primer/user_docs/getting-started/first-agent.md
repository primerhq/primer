---
slug: first-agent
title: Build your first agent
section: getting-started
summary: A five-minute speedrun from install to a working agent answering a question.
---

## Goal

By the end of this page you have an agent named `helper`, bound to
the system toolset, that can answer a single question from the
console or the REST API.

## Step 1: Create the agent

Open the Agents page from the left nav, hit Create. The modal opens
on the Basic tab.

```mockup:agent-create-modal
{ "tab": "basic" }
```

Fill in:

- Name: `helper`
- Description: anything; this only shows on the agents list.
- Model: leave the default (`claude-opus-4-8`).

Then switch to the Tools tab and check `system`. Leave the prompt
tab empty for now; the default prompt is good enough for the
speedrun.

Click Create. The modal closes and the agent shows up in the list.

## Step 2: Invoke from the console

Hit New session from the Sessions page. Pick `helper` from the
agent picker, type a question into the first turn, hit Send. The
session detail page streams the model's reply.

## Step 2 alternative: Invoke from the REST API

Same operation, different surface:

```code-tabs:python,curl
--- python
import primer
client = primer.Client(token="...")
sess = client.sessions.create(agent_id="helper")
turn = client.sessions.turn(
    sess.id,
    input="What is the capital of France?",
)
print(turn.output)
--- curl
SID=$(curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -X POST http://localhost:8000/v1/sessions \
  -d '{"agent_id":"helper"}' | jq -r .id)

curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -X POST http://localhost:8000/v1/sessions/$SID/turn \
  -d '{"input":"What is the capital of France?"}'
```

The session id is durable: subsequent turns against the same id
extend the transcript.

## Where to next

```ref:features/agents
The feature-level walkthrough explains every knob on the create
modal in detail.
```

```ref:concepts/what-is-an-agent
The concepts page covers the turn loop and where state lives.
```

```callout:tip
Once the speedrun works end to end, swap the system toolset for a
narrower one and try a real task. The default prompt is fine for a
demo; production prompts deserve more thought.
```
