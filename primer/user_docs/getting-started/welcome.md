---
slug: welcome
title: Welcome to primer
section: getting-started
summary: What primer is, who it is for, and how these docs are organised.
---

## What is primer?

Primer is a platform for building and running LLM agents in production. It gives operators a console, an HTTP API, and an MCP interface to:

- **Define agents** -- configure an LLM provider, attach toolsets, set a system prompt, and version the result.
- **Run sessions** -- every agent run is a session tracked end-to-end with tool calls, approvals, and output.
- **Build graphs** -- wire agents together into multi-step pipelines where the output of one node feeds the next.
- **Chat** -- expose agents as streamed chat endpoints for end-user or automated clients.
- **Manage workspaces** -- give agents a sandboxed environment (container, k8s pod, or local directory) to operate in safely.
- **Set up triggers** -- schedule or event-drive agent sessions so they run without manual intervention.
- **Route with channels** -- route messages and tool results across named channels to decouple producers from consumers.

Primer is aimed at operators who manage the infrastructure: install, configure providers, create agents, and connect everything. End-users and the agents themselves interact through the API and MCP surface; this doc set describes the operator view.

## How the docs are organised

| Section | Contents |
|---|---|
| **Getting started** | Install, first login, and your first agent. Start here. |
| **Concepts** | The data model -- agents, sessions, workspaces, graphs, channels, triggers. |
| **Features** | Detailed guides for each capability: auth, approvals, knowledge, toolsets, and more. |
| **Cookbook** | End-to-end recipes for common setups. |
| **Reference** | CLI flags, environment variables, and REST API surface. |

```callout:tip
These docs live inside the console itself. Hit Cmd+K to jump to any doc by title from anywhere in the console.
```

## Where to next

Install primer and bring up the API server:

```ref:getting-started/install
How to install primer and start the API server.
```

Then run through creating your first agent:

```ref:getting-started/first-agent
Create an agent, run a session, and see the result.
```
