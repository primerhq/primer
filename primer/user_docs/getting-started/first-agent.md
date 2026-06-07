---
slug: first-agent
title: Build your first agent
section: getting-started
summary: End-to-end console walkthrough -- configure a provider, create an agent, and watch it run.
---

## Goal

By the end of this page you have an agent named `helper` running its
first session entirely from the console. No API calls required for the
first win.

## Step 1: Configure an LLM provider

Before creating an agent, primer needs at least one LLM provider
configured. Go to **Providers** in the left nav, then **LLM**, and
click **Add provider**. Supply the provider type (for example
`anthropic` or `openai`), a display name, and your API key. Enable
at least one model on the provider row -- this is the model name the
agent will reference.

```callout:info
If primer was installed with auto-bootstrap enabled (the default), a
placeholder provider entry may already exist. Edit it to fill in a
real API key and enable a model before proceeding.
```

## Step 2: Create the agent

Open **Agents** from the left nav and click **New agent**. The
three-tab create modal opens.

```embed:agents-page
```

Fill in the **Basic** tab:

- **Description** -- a short label shown in the agents table (for
  example `My first helper`).
- **LLM provider** -- select the provider you configured in Step 1.
- **Model** -- the dropdown populates from the selected provider.
  Pick the model you enabled.

Switch to the **Tools** tab. Check the `system` toolset to give the
agent shell and filesystem access inside its workspace.

```callout:warning
The `system` toolset grants shell and filesystem access. Pair it with
a tight workspace template when prototyping; relax limits only after
validating the prompt and tool use pattern.
```

Switch to the **Advanced** tab to enter a system prompt. The system
prompt is a list of segments joined at runtime -- start with a single
segment such as:

> You are a concise assistant. Answer clearly and briefly.

Leave compaction prompt and temperature at their defaults for now.
Click **Create**. The modal closes and the agent row appears in the
agents list.

## Step 3: Start a session

Go to **Sessions** in the left nav and click **New session**. Select
`helper` from the agent picker. Type your first message -- for
example, "What files are in the current directory?" -- and click
**Send**.

```embed:sessions-list
```

Primer queues the turn and the worker picks it up. The session detail
page streams the model reply as tokens arrive.

```callout:tip
The session id is durable. Return to this session from the Sessions
list at any time; subsequent messages extend the same transcript.
```

## Where to next

```ref:features/agents
The Agents feature page covers every field in the create modal,
health checks, editing, and harness-managed agents.
```

```ref:concepts/what-is-an-agent
The concept page explains the turn loop and how agents relate to
sessions, workers, and workspaces.
```

```ref:reference/api-agents
Automate this: full resource schema, list/create/update/delete
endpoints, and the status check endpoint.
```
