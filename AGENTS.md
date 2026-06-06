# AGENTS.md - Primer for AI agents

Welcome. You're reading this because an MCP client connected you to a
primer deployment. This document is the **one-page orientation**:
what primer is, how the MCP contract works, and how to look up
detail on any capability. The detail docs themselves live inside
primer's `_internal_ai_docs` collection and are addressable through
two MCP tools you'll learn about below.

---

## 1. What primer is

Primer is a **multi-agent orchestration platform**. Its primary
abstractions are:

- **Agents** - LLM-backed workers with a system prompt, a tool set,
  and an LLM provider. Each agent runs inside a context (a chat for
  interactive turns; a session for headless work; a graph node for
  multi-step orchestration).
- **Workspaces** - isolated filesystems plus git-tracked state that
  sessions run inside. The unit of execution isolation.
- **Chats** - multi-turn human-in-the-loop conversations with an
  agent. Each turn is worker-claimed; turns survive disconnects.
- **Sessions** - long-running headless agent runs in a workspace.
  Can pause, resume, and yield on external events.
- **Graphs** - directed graphs of agents (and other node types) with
  conditional routing. Run to completion in one invocation.
- **Triggers and subscriptions** - event scheduling (delayed, cron,
  channel) that dispatches to chats, fresh sessions, or parked
  yielding tools.
- **Collections and documents** - knowledge containers with
  embedding-backed semantic search. Plus a parallel
  **internal-collections** subsystem that auto-indexes primer's own
  agents, graphs, tools, collections, and these very docs for
  semantic discovery.
- **Channels** - Slack/Telegram/Discord bridges that forward
  `ask_user` prompts and tool-approval requests, and source
  channel-driven triggers.
- **Harnesses** - git-installed bundles of agents/graphs/etc., with
  per-deployment overrides; install / sync / uninstall lifecycle.

These compose. A typical primer-driven workflow looks like: a
**trigger** fires on a schedule, dispatches a fresh **session** in
a **workspace**, the session runs an **agent** that calls tools (some
of which require **tool approval**), the agent yields on `ask_user`,
the prompt is **channel**-forwarded to Slack, the human replies, the
session resumes, the agent finishes, the workspace persists the
result. Everything in that flow has a corresponding doc you can pull
in detail from.

## 2. The MCP contract you're talking to

You're connected to primer's MCP endpoint at `/v1/mcp` over
Streamable HTTP. Your auth is one of two things:

- A **bearer token** (`Authorization: Bearer <plaintext>`). Tokens
  carry a scope list; you need the `mcp` scope to reach this
  endpoint. Tokens are operator-minted and revocable.
- A **cookie session** (rare for external clients; mostly the
  operator console). Cookies bypass scope checks.

The tools you can call are exactly the intersection of three sets:

- **Allowlist.** The operator's `McpExposure.allowed_tools` list.
  Tools not in it are unreachable. If `tools/list` returns empty,
  the operator hasn't enabled exposure or hasn't allowlisted
  anything yet.
- **Exposability.** Yielding tools (`ask_user`, `subscribe_to_trigger`,
  `_approval`) are silently dropped - MCP has no pause/resume.
  Session-bound workspace tools (e.g. `watch_files`) are dropped
  too. Tools with `required` tool approval policies are silently
  dropped (operator owns visibility decisions there).
- **Scope.** Bearer tokens without `mcp` get 401 before they ever
  see `tools/list`.

Tool IDs are scoped: `<toolset_id>::<tool_id>` (e.g.
`system::list_agents`, `search::search_ai_docs`,
`workspaces::read_workspace_file`). The double-colon is reserved.

Error envelopes from `tools/call`:

```json
{
  "is_error": true,
  "output": "{\"type\":\"<error-type>\",\"message\":\"<human-readable>\"}"
}
```

Common error types: `tool-not-allowed`, `validation-error`,
`subsystem-inactive`, `not-found`, `auth-error`, `tool-error`.

## 3. How to learn more - the documentation contract

Primer's full feature documentation lives in the `_internal_ai_docs`
reserved collection. Each capability has one document; the document
id is its **slug** (lowercase, kebab-case). Two MCP tools give you
access:

- **`search::search_ai_docs`** - semantic search. Embeds your query
  and returns ranked chunks (Markdown sections) of matching docs.
  Each hit carries `document_id` (the slug), `chunk_id` (which
  section), a similarity `score`, the `text` of the matched chunk,
  and meta with the doc's title, summary, and full mcp_tools list.
  Use this when you know what you want to do but not which doc
  covers it.

- **`system::get_document_content`** - fetch a doc by id. Returns
  `{id, collection_id, name, content}`. The content is the full
  Markdown source of the doc. Use this when search has identified
  the relevant slug and you want the complete doc, not just a
  matched chunk.

Both tools are read-only and idempotent; calling them never changes
primer state.

If `search::search_ai_docs` returns `is_error=true` with
`subsystem-inactive`, the internal-collections subsystem hasn't
been bootstrapped on this deployment. In that case, you can still
read individual docs by their slug via `system::get_document_content`
(passing `id=<slug>`, e.g. `id="agents"`) - the Document rows exist
in storage regardless of bootstrap status; only the search index
is gated.

### Capability index

Every entry below is one document in `_internal_ai_docs`. Fetch the
full doc with
`system::get_document_content(id="<slug>")`. Search for a topic with
`search::search_ai_docs(query="<terms>")`.

| Slug | What it covers |
| --- | --- |
| `agents` | Agent definition, system prompts, tool sets, LLM config, response formats, the turn-loop runtime, auto-compaction. |
| `graphs` | Multi-step agent orchestration. Nodes (agent/task/subgraph/http/callable_router), edges (static/json_path/callable_router), supersteps, cycles. |
| `workspaces` | Execution sandboxes. Workspace lifecycle, providers, templates, `.state/` and `.tmp/` layout, multi-session coordination, MCP file I/O tools. |
| `sessions` | Long-running headless agent runs. Status enum (RUNNING/WAITING/PAUSED/ENDED), pause/resume/end controls, `waiting.json` contract. |
| `chats` | Multi-turn conversations. Turn claim mechanics, message kinds, WS protocol, cursor reconnect, drain loop, cancellation, auto-compaction. |
| `knowledge` | User-defined collections and documents. CRUD, content storage (`meta.content`), ingest pipeline status, `get_document_content`. |
| `semantic-search` | Internal collections (`_internal_*`), the IC subsystem, the `search::*` toolset, including this doc collection itself. |
| `triggers-and-subscriptions` | Event scheduling. Trigger kinds (delayed/scheduled/channel), subscription kinds (chat_message/agent_fresh_session/graph_fresh_session/parked_session), fire semantics, catch-up policy. |
| `yielding` | The park/resume primitive. `Yielded` sentinel, parked state fields, event keys, timeout sweeper, cancellation; why yielding tools are invisible from MCP. |
| `tool-approval` | Pre-dispatch gates on tool calls. Approval kinds (required/policy/llm), fail-closed semantics, supersession by new user turns, interaction with MCP exposure. |
| `harnesses` | Git-installed entity bundles. Lifecycle (draft → ready → installed → outdated), fetch/install/sync/uninstall, managed entity guard. |
| `channels` | Slack/Telegram/Discord adapters. ChannelProvider → Channel → WorkspaceChannelAssociation, forwarding flags, first-reply-wins, fire-and-forget posts. |
| `mcp-exposure` | The MCP server endpoint itself. The `McpExposure` singleton, the allowlist, scope auth, hard-deny (none in v1), GZip bypass. |
| `auth-and-tokens` | API tokens, scopes, cookie vs bearer, token minting, revocation, the `mcp` scope. |

### Quick-pick by goal

- **"I want to find a tool that does X."** Call
  `search::search_tools(query="<terms>")`. Returns ranked tool ids.
- **"I want to find an agent that does X."** Call
  `search::search_agents(query="<terms>")`.
- **"I want to find a graph that does X."** Call
  `search::search_graphs(query="<terms>")`.
- **"How does X work?"** Call
  `search::search_ai_docs(query="<terms>")`, then fetch the matched
  doc with `system::get_document_content(id="<slug>")`.

## 4. A minimal first-touch workflow

If this is your first time on this primer deployment, this is the
shortest path to understanding what's available:

1. **List the tools you have.** Call `tools/list`. The shape of
   that list - what's in the allowlist, what's missing - tells you
   the operator's intent for your role.

2. **Sanity-check the search subsystem.** Call:
   ```json
   {"tool": "search::search_ai_docs", "arguments": {"query": "overview", "top_k": 3}}
   ```
   If it returns hits, semantic search is live and you can navigate
   the docs by topic. If it returns
   `is_error=true type=subsystem-inactive`, fall back to fetching
   docs by slug.

3. **Enumerate the catalogue.** Brief picture of what's installed:
   - `system::list_agents` - what agents are defined.
   - `system::list_graphs` - what graphs are defined.
   - `system::list_workspaces` - what workspaces exist.
   - `system::list_collections` - what knowledge is curated.

4. **Read the docs relevant to the user's goal.** Pick from the
   capability index. Don't read all 14 - read the 1-3 that match
   the work the user has asked for.

## 5. Things primer can't do (yet)

- **`system::search_collection`** - searching documents *within* a
  user-defined collection - is a stub. Use
  `system::find_collection_documents_by_meta` or read by id.
- **Mid-graph yield/pause** - graphs run to completion in one
  invocation. For human-in-the-loop multi-step work, use chats or
  sessions with the `subscribe_to_trigger` yielding tool.
- **Yielding tools over MCP** - `ask_user`, `subscribe_to_trigger`,
  `_approval` are not exposed to MCP clients. Poll instead, or rely
  on primer's own internal agents.
- **Live document upload (multipart `POST /v1/documents` with
  embedding)** - deferred. `POST /v1/documents` persists the row;
  vectorising is a follow-up.

These are visible blanks; mention them to your user when relevant
rather than pretending the workaround is the intended path.

---

When in doubt: search the docs, then read the matched doc fully.
Don't reason from the snippet alone - the Gotchas section at the
bottom of each doc usually contains the one thing that catches
people, and the snippet won't include it.

---

## For contributors (developers and coding agents)

The two sections above orient MCP clients calling a running primer
deployment. If instead you are modifying the primer codebase, the
authoritative developer reference lives under `docs/dev/`:

- Start at `docs/dev/README.md` for the doc-set conventions and the
  subsystem dependency graph.
- Read `docs/dev/CONTRIBUTING.md` before making any change. It carries
  the required reading order, the five-track completeness checklist
  (backend, frontend, MCP tools, tests, docs), the PR conventions, and
  the common pitfalls.
- `docs/dev/architecture/` documents the cross-cutting patterns
  (storage, rest-api, claim-machine, worker-system, provider-pattern,
  observability, auto-bootstrap). `docs/dev/subsystems/` documents each
  feature and the patterns it consumes.

Standing repo rules for any change:

- Conventional commit messages. No `Co-Authored-By` footer.
- No force-push to `main`.
- The narrowed sweep stays green at every commit:
  `uv run pytest tests/ -q --ignore=tests/distributed --ignore=tests/ui_e2e --ignore=tests/e2e --ignore=tests/integration --ignore=tests/llm`.
- No em dash characters anywhere in committed files (the
  `tests/docs/` hygiene suite enforces this for `docs/dev/`).
- Restart `uv run primer api` at the end of any code-changing task and
  confirm `/v1/health` returns 200.

Bugs filed through the in-UI reporter land under `~/.primer/bugs/`. An
open bug has `meta.json.status == "open"`; fixing one updates the meta
with `status`, `fixed_at`, and `commit_sha`.
