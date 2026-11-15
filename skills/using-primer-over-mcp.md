# Using a primer deployment over MCP

> This guide is for an AI agent that has been connected to a running primer
> deployment over MCP and wants to USE it. If instead you are modifying the
> primer codebase, read `AGENTS.md` at the repo root (the contributor guide).
>
> (Moved out of AGENTS.md, which is now the contribution guide.)

# AGENTS.md - Primer for AI agents

Welcome. You're reading this because an MCP client connected you to a
primer deployment. This document is the **one-page orientation**:
what primer is, how the MCP contract works, and how to look up
detail on any capability. The detail docs themselves live inside
primer's reserved `system` collection (a read-only wiki of the
platform) and are addressable through two MCP tools you'll learn
about below.

---

## 1. What primer is

Primer is a **multi-agent orchestration platform**. Its primary
abstractions are:

- **Agents** - LLM-backed workers with a system prompt, a tool set,
  and an LLM provider. Each agent runs inside a context (a session in
  a workspace for interactive or headless turns; a graph node for
  multi-step orchestration).
- **Workspaces** - isolated filesystems plus git-tracked state that
  sessions run inside. The unit of execution isolation.
- **Sessions** - agent runs in a workspace, long-running headless or
  interactive multi-turn (messages can be sent to a running session).
  Can pause, resume, and yield on external events.
- **Graphs** - directed graphs of agents (and other node types) with
  conditional routing. Run to completion in one invocation.
- **Triggers and subscriptions** - event scheduling (delayed, cron,
  channel) that dispatches to fresh agent or graph sessions, parked
  sessions, or appends to an existing session.
- **Collections and documents** - knowledge containers with
  embedding-backed semantic search. Plus the reserved **`system`
  collection**, a read-only wiki primer writes itself: guides, a
  reference page per tool, these very docs and a live map of this
  install's agents, graphs and collections. It is searchable by
  keyword, and by meaning once an admin turns internal search on.
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
  carry a scope list. A bearer without `mcp` can still connect and see
  the three meta-tools, but the `mcp` scope is what gates discovery
  and calls. Tokens are operator-minted and revocable.
- A **cookie session** (rare for external clients; mostly the
  operator console). Cookies bypass scope checks.

The endpoint advertises exactly three meta-tools - never the target
catalogue:

- **`list_tools`** - page the catalogue you are authorized to see,
  with deterministic filters and a keyset cursor (`cursor`, `limit`,
  `include_schema`).
- **`find_tools`** - search that catalogue by words, with optional
  semantic backing over the `system` wiki's tool reference pages.
- **`call_tool`** - call one discovered tool by its scoped id with its
  arguments: `{"scoped_id": "system__wiki_read", "arguments": {...}}`.

Every `toolset::tool` named in this guide is called that way: pass the
`__`-scoped id to `call_tool`. Tool ids are scoped: over the wire a
tool is named `<toolset_id>__<tool_id>` (e.g. `system__list_agents`,
`system__wiki_search`, `collections__search`); this guide and the docs
also write the same tool as `<toolset_id>::<tool_id>`
(`system::list_agents`). Discovery is caller-aware: `list_tools`
and `find_tools` never reveal an id you cannot use, and an empty
catalogue can mean the endpoint is disabled, your token lacks `mcp`,
your role or toolset grants narrow everything away, or an allowlist
mode excludes every tool - the answer does not say which.

What a caller may use is the intersection of:

- **Exposure mode.** `rbac` (the default): every otherwise-authorized
  reserved built-in and generated tool clears the caller's role floor
  and toolset grants. `allowlist`: the scoped id must additionally be
  in the operator's saved `allowed_tools` set; an empty allowlist is
  not allow-all.
- **Exposability.** Yielding tools (`ask_user`, `subscribe_to_trigger`,
  `_approval`) and session-bound workspace tools (e.g. `watch_files`)
  are never surfaced by discovery, and `call_tool` refuses them - MCP
  has no pause/resume. Tools with a `required` tool approval policy are
  never surfaced either, and `call_tool` refuses them with the fixed
  `tool_unavailable` envelope (the audit/event reason is
  `approval_required`; the operator owns visibility decisions there).
  That includes every tool that creates, updates or deletes an agent,
  a graph or a trigger (`system::create_agent`, `system::update_graph`,
  `trigger::create`, the deletes and the `crud` tools), every approval
  policy write (`system::create_tool_approval_policy` and its update
  and delete), a `system::create_toolset` or `update_toolset` whose
  body is a Python or stdio MCP toolset (an http or sse one goes
  through), and `harness::harness__install`, `harness__sync` and
  `harness__uninstall`: they are gated by default, whatever the
  caller's role, so over MCP they are refused with the fixed
  `tool_unavailable` envelope (the audit/event reason is
  `approval_required`) unless the operator disables or retunes their
  gate. The exposure
  surface publishes reserved built-in and generated tools only; a
  user-registered toolset's tools are never callable over MCP.
- **Scope.** Bearer tokens without `mcp` still connect and see the
  three meta-tools; their catalogue is empty (the scope gate runs at
  the discovery seam) and every `call_tool` answers the fixed
  `tool_unavailable` envelope. The gate runs at discovery and call
  time, never at connect time.

`call_tool` rechecks all of this - live exposure mode, allowlist,
role, toolset grants, scope and the approval gate - at call time; a
listed id can be refused the next second.

Refusals answer a fixed envelope instead of the inner detail: an
unavailable or unseen target answers
`{"ok": false, "error": {"code": "tool_unavailable", "message": "Tool is unavailable."}}`
(as `isError`), a role or scope denial answers `access_denied`,
arguments that do not match the target's schema answer
`invalid_arguments`, and a target that raised answers `tool_failed`
with a bounded, credential-scrubbed message. A name that is not one of
the three meta-tools answers JSON-RPC method-not-found with the same
fixed message as a refused tool, and outer failures are fixed text too
(`Input validation error: arguments`, `Tool dispatch failed.`) - never
an echo of your input.

## 3. How to learn more - the documentation contract

Primer's full documentation lives in the reserved `system` collection, a read-only platform wiki. Read `index` for its table of contents, `purpose-map` for goals, or a feature path such as `agents/create-and-edit`. Each exact tool has a generated reference at `reference/tools/<toolset>/<tool>`. `this-install/` is the live map of this install's agents, graphs, collections, workspaces and providers.

- **`system__wiki_search`** takes `query`, optional `mode` (`auto`, `semantic`, `fulltext` or `literal`), `path_prefix` and `limit`. It returns `{hits, truncated, mode_used}`. With no working semantic index, use auto for lexical discovery. The live map is excluded unless the prefix requests it.
- **`system__wiki_read`** takes a canonical path, for example `{"path": "agents/create-and-edit"}`; an empty path reads the table of contents. It returns `{path, title, summary, kind, body, children}`. Read the complete page after search has identified it.

These are target ids: an outside client discovers them through the three MCP meta-tools, then calls a returned scoped id through `call_tool`. Both targets are read-only. Ordinary collection search/read on `collection: system` reaches the same pages. Reading by path never depends on a semantic index.

The loop is: search for the goal, read the matching feature or runbook, inspect the exact tool reference and live describe facts, then make the authorized call. Feature pages use What it is, Concepts, How to, Tool calls, Gotchas and Related; recipes use Goal, Prerequisites, Steps, Verify, Gotchas and Related. A reference supplies actual argument, role, default policy and result facts. A default policy is not a promise that a call is currently ungated.

### Capability index

| Goal | Canonical wiki path |
|---|---|
| Define an agent, prompts, tools or compaction | `agents` |
| Create and run a graph, including human answer routing | `graphs`, `graphs/ask-a-person` |
| Create workspaces, use files, locks and templates | `workspaces`, `workspaces/write-safety` |
| Start, observe, steer or resume a session | `sessions` |
| Work with collections, stored bodies and search | `knowledge` |
| Schedule or subscribe to events safely | `triggers`, `triggers/event-security` |
| Understand approvals and hidden card arguments | `approvals`, `approvals/decision-context` |
| Install or sync a harness | `harnesses`, `runbooks/install-a-harness` |
| Configure channels and reply binding | `channels`, `runbooks/onboard-a-channel` |
| Manage models, search or artifact providers | `providers` |
| Understand speech as an edge transform | `providers/speech` |
| Publish and activate service bundles | `workspaces/services` |
| Connect external clients or upstream MCP servers | `toolsets/mcp-exposure`, `toolsets/sign-in` |
| Understand accounts, scoped tokens and welcomes | `start-here/accounts-and-tokens` |
| Find human sign-in or deployment boundaries | `not-agent-callable` |

### Recipes by goal

Read `cookbook` for complete recipes and `runbooks` for the sampled lifecycle contracts. Recipe paths have no extra prefix: for example `cookbook/create-and-run-a-session`, `cookbook/monitor-and-resume-a-parked-session`, `cookbook/multi-agent-graph-research` and `cookbook/internal-tool-via-mcp`. A parked turn usually keeps `status: running` with park fields; waiting between turns is a different state. Follow the page's actual pending-response instructions rather than answering a park with a steer or peer message.

## 4. A minimal first-touch workflow

If this is your first time on this primer deployment, this is the
shortest path to understanding what's available:

1. **Learn the three meta-tools.** `tools/list` always names exactly
   three tools: `list_tools`, `find_tools`, `call_tool` - never the
   target catalogue. The catalogue you may actually call is yours
   alone: `list_tools` pages it (filters, `include_schema`) and
   `find_tools` searches it by purpose. An empty catalogue means the
   operator's mode, your token's scope, or your role and grants
   narrowed everything away - not that there are no tools.

2. **Read the table of contents.** Call `call_tool` with
   `{"scoped_id": "system__wiki_read", "arguments": {"path": ""}}`.
   It lists the wiki's sections, the tool reference, the live install
   map and the agent docs. Then search by topic with
   `system::wiki_search`; `mode_used: "fulltext"` in its answer means
   internal search is off and the search matched keywords only.

3. **Enumerate the catalogue.** Brief picture of what's installed:
   - `system::list_agents` - what agents are defined.
   - `system::list_graphs` - what graphs are defined.
   - `workspaces::list_workspaces` - what workspaces exist.
   - `system::list_collections` - what knowledge is curated.

4. **Read the docs relevant to the user's goal.** Pick from the
   capability index. Don't read them all - read the 1-3 that match
   the work the user has asked for.

### Run an agent or graph over MCP

Once you have picked an agent or graph (see the capability index and
`cookbook/discover-a-capability`), you can run it end to end:

1. `workspaces::create_workspace` (from a `template_id`) - returns the workspace id.
2. `workspaces::create_workspace_session` with `binding` set to
   `{"kind":"agent","agent_id":"..."}` or `{"kind":"graph","graph_id":"..."}`,
   `initial_instructions` (or `graph_input`), and `auto_start: true` - returns the session id.
3. Poll `workspaces::get_workspace_session` until `status` is `ended`
   (`ended_reason: completed`). A `waiting` status means the agent parked on a
   yielding tool; see `cookbook/monitor-and-resume-a-parked-session`. A `waiting`
   status whose `last_turn_error` is set (`{code, at}`) is NOT a park: the last turn
   failed with a transient model error and the session rests; send a message to continue
   it, or treat the run as failed.
4. Read output files with `workspaces::read_workspace_file`, and what the session said with
   `workspaces::read_workspace_session_messages` (poll with `after_seq`). Never read
   `.state/` or `.tmp/` paths: raw reads there are admin-only.
5. End a run with `workspaces::cancel_workspace_session`; to stop only its current turn and keep the
   session, use `workspaces::interrupt_workspace_session` (a success answer alone is not a Stop: check
   `interrupt_requested` in it). Clean up with `workspaces::delete_workspace`.

Full recipe: `cookbook/create-and-run-a-session`.

## 5. Things primer can't do (yet)

- **`system::refresh_collection`** - re-embedding a collection's
  documents after swapping the embedder - is a stub: it returns
  `type=not-implemented` until the SearchService ingestion pipeline
  lands. `system::search_collection` is not a stub: it runs real
  semantic search over a collection's document contents when search
  is enabled on the collection.
- **Mid-graph yield/pause** - graphs run to completion in one
  invocation. For human-in-the-loop multi-step work, use sessions
  with the `subscribe_to_trigger` yielding tool.
- **Yielding tools over MCP** - `ask_user`, `subscribe_to_trigger`,
  `_approval` are never surfaced by discovery, and `call_tool`
  refuses them (no pause/resume over MCP in v1). Poll instead, or
  rely on primer's own internal agents.
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
