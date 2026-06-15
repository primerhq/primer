---
slug: toolsets-overview
title: Toolsets & Tools
section: toolsets
summary: How tools are grouped into toolsets, the difference between built-in system toolsets and externally-registered ones, and how approvals gate tool calls.
---

## Tools and toolsets

A **tool** is one callable function an agent can invoke during a turn: it has an id, a JSON argument schema, and a handler. A **toolset** is a named collection of related tools. An agent is bound to toolsets (not individual tools at the wire level), and its effective tool list is the union of every toolset it has.

Tool ids are scoped by their toolset using a double-underscore separator, for example `system__invoke_agent`, `search__search_agents`, or `web__web_search`.

## Two kinds of toolset

- **System (built-in) toolsets** ship with primer and are always available: agents, search, workspaces, web, and the rest. They are reserved; you cannot create or delete them, only bind agents to them and pick which tools are exposed.
- **External toolsets** are registered by you and pull tools in from outside primer over a transport. Today the one external kind is MCP (Model Context Protocol) servers.

Both kinds appear in the same toolset list and bind to agents the same way.

## Approvals

Any tool call can be gated behind an approval policy, so a human (or another agent) must sign off before the call runs. Approvals are configured per agent and per tool, independent of which toolset the tool came from.

```ref:toolsets/toolsets-system
The built-in system toolsets and how to explore tools with list_toolset_tools and call_tool.
```

```ref:toolsets/toolsets-external
Register external toolsets (MCP servers) over stdio and HTTP so agents can call their tools.
```

```ref:toolsets/toolsets-approvals
Gate tool calls behind an approval policy and decide each request before it runs.
```
