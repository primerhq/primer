---
slug: agents
title: Agents
section: features
summary: Create and manage agents in the console -- configure a provider, bind tools, and set a system prompt.
---

## Overview

An agent pairs an LLM provider and model with a set of tools and a system prompt. The Agents page lists every defined agent with its provider, model, tool count, session count, and a live status indicator. Click "New agent" to open the three-tab create modal.

```embed:agents-page
```

## Creating an agent

1. Open the Agents page from the left nav.
2. Click "New agent" (top-right of the filter bar).
3. In the **Basic** tab, fill in:
   - **ID** -- optional; the backend assigns one if left blank.
   - **Description** -- a short label shown in the agents table.
   - **LLM provider** -- pick from the providers configured under /providers/llm. If the list is empty, create a provider there first.
   - **Model** -- the dropdown populates from the selected provider row.
4. Switch to the **Tools** tab. Use the search box to filter by tool name, description, or toolset. Check individual tools or use the toolset header checkbox to bulk-select all tools in a toolset. The counter at the top right shows how many tools are selected.
5. Switch to the **Advanced** tab if you need to set a system prompt, compaction prompt, or temperature (all optional).
6. Click **Create**. The modal closes and the new agent row appears in the list; the page navigates to the agent detail view.

```callout:warning
Binding tools from the `system` toolset gives the agent shell and filesystem access through the sandboxed workspace. Pair it with a tight workspace template (small memory, no network egress, short TTL) when prototyping. Relax limits only after validating the prompt and tool use pattern.
```

## Editing an agent

Open the agent detail, go to the **Config** tab, and click **Edit**. The same three-tab modal opens with existing values pre-filled. The ID field is locked after creation.

Agents managed by a harness show a notice on the Config tab -- edit the harness instead of the agent directly.

## Checking agent health

The detail page shows a status panel at the top. It calls `GET /v1/agents/{id}/status` and reports whether the bound provider and toolsets all resolve. A red banner lists specific issues blocking new sessions.

## Automate this

```ref:reference/api-agents
Full resource schema, list/create/update/delete endpoints, and status check.
```

## See also

```ref:concepts/what-is-an-agent
The concept page explains the turn loop and how agents relate to sessions, workers, and workspaces.
```

```ref:features/agents-advanced
Model selection, compaction prompts, fine-grained tool binding, and the retry loop.
```

```ai-doc:agents
```
