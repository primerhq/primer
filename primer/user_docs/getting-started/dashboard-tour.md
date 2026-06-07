---
slug: dashboard-tour
title: Dashboard tour
section: getting-started
summary: A guided tour of the primer console -- left nav sections, top bar, and the health view.
---

## The top bar

The top bar is fixed at the top of every console page. From left to right: the primer brand mark and hostname, the command palette trigger (Cmd+K or Ctrl+K), the worker pool pill, the light/dark toggle, and the signed-in user avatar.

The worker pill shows `active/total workers . N in flight`. It turns amber when the pool is near capacity (80% or above) and red when no workers are active. Click it to jump directly to the Workers page.

An amber bell button appears in the top bar only when the Internal Collections subsystem is configured but has not been bootstrapped yet. Click it to go to the Internal Collections page and complete the setup.

## Left navigation

The sidebar organises every console area into labelled groups. Each group is collapsible; the collapsed state persists in localStorage. In icon-only mode the labels hide and a collapse toggle at the foot of the sidebar restores them.

### Compute

| Item | What it shows |
|---|---|
| Sessions | All agent sessions, live and historical. A count badge reflects the current total. |
| Agents | Registered agent definitions. |
| Graphs | Multi-agent flow definitions. |
| Chats | WebSocket-backed conversational sessions. Count badge reflects live total. |
| Approvals | Tool-call gating queue. A count badge shows pending approvals across sessions and chats. |

```embed:agents-page
```

```ref:features/agents
Deep dive into agent configuration and lifecycle.
```

```ref:features/sessions
How sessions are created, claimed by workers, and terminated.
```

```ref:features/chats
Conversational sessions and the WebSocket stream.
```

```ref:features/tool-approval
Approval policies and the gating queue.
```

### Knowledge

| Item | What it shows |
|---|---|
| Collections | Vector stores, each bound to one embedding provider. |
| Documents | All ingested documents across collections. |
| Internal Collections | The semantic search subsystem that indexes agents, graphs, and tools. Shows an ON/OFF pill. |

```ref:features/knowledge-collections
Managing collections and ingesting documents.
```

```ref:features/internal-collections
Bootstrapping the internal semantic index.
```

### Workspaces

| Item | What it shows |
|---|---|
| Workspaces | Materialised workspace instances with their bound sessions. Count badge reflects live total. |
| Templates | Declarative recipes for materialising workspaces. |
| Providers | Backend configurations that templates resolve to. |

```ref:features/workspaces
Workspace types, templates, and provider configuration.
```

### Web

Web Search shows the active search provider configuration. DuckDuckGo is built in; Tavily is configurable.

### Toolsets

| Item | What it shows |
|---|---|
| Toolsets | Built-in primitive groups and user-registered MCP servers. |
| Tools | Every tool exposed by every toolset, with per-tool approval policy. |

### Providers

| Item | What it shows |
|---|---|
| LLM | Configured language model providers. |
| Embedding | Embedding providers used by collections. |
| Cross-Encoder | Re-ranking providers. |
| Semantic Search | pgvector or pgvectorscale indexes for collections. Count badge reflects configured providers. |
| Channels | Messaging provider adapters (Slack, Telegram, Discord). |

### Communication

| Item | What it shows |
|---|---|
| Channels | External rooms, DMs, and chats bound to a provider. Count badge reflects live total. |
| Associations | Which workspaces fan out to which channels, with per-tool flags. |

```ref:features/channels
Configuring channel providers and associations.
```

### Distributions

Harnesses holds test harness definitions used for end-to-end evaluation runs.

### Automation

Triggers holds delayed and scheduled dispatches to subscriptions.

```ref:features/triggers
Creating and managing triggers.
```

### Operations

```embed:workers-stats
```

| Item | What it shows |
|---|---|
| Workers | Live worker pool -- capacity, in-flight count, and per-worker status. Auto-refreshes every 2 seconds. |
| Health | The full `/v1/health` response rendered as a dashboard, polled every 5 seconds with a client-side history graph. |

```ref:features/workers-and-health
Understanding worker pool health and the health endpoint.
```

### Account

| Item | What it shows |
|---|---|
| API Tokens | Bearer credentials for programmatic clients -- scoped, revocable, and audit-logged. |
| MCP Server | Expose a subset of primer tools to MCP clients via the streamable HTTP endpoint at `/v1/mcp`. |

```ref:features/auth-and-tokens
Creating and rotating API tokens.
```

### Help

Docs opens the full operator guide inside the console. A thin top bar provides a back-to-console link and a shortcut to the REST API reference (opens in a new tab). The command palette (Cmd+K) also searches doc titles and summaries.

## The dashboard landing page

Opening the console at `/` (or navigating to Dashboard) shows the operator overview. The page sub-header shows `primer . localhost:8765`. A "New session" button in the top-right of the page header opens the agent picker modal.

The dashboard tiles poll `/v1/workers` and `/v1/health` every 5 seconds. Worker counts are authoritative from the live API; there is no mock fallback.

```ref:getting-started/first-agent
Build your first agent in 5 minutes.
```
