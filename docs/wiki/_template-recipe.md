---
title: "Recipe title"
summary: "One line: what the recipe achieves."
order: 99
tools: [workspaces__create_workspace]
related: [workspaces/create-and-delete]
---
## Goal

One or two sentences: the result, and who it is for.

## Prerequisites

- What must exist first (an agent, a template, a model profile) and the page or call that makes it.

## Steps

### 1. Name the step

One sentence on what the call does, then the call:

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace", "arguments": {"template_id": "py-base"}}
```

Response:

```json
{"id": "ws-1", "phase": "running"}
```

Say which value of the response the next step uses.

## Verify

How to see that it worked: the state or the file to check.

## Gotchas

- What fails in practice, and what to do about it.

## Related

- [Page title](section/page)
