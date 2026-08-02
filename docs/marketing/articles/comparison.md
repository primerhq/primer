<!-- DRAFT — founder to review + fact-check competitor claims (see editor notes at bottom) before publishing on own docs/blog. Purpose: the page you link when someone asks "why not just LangGraph?" -->

# Primer vs. LangGraph, CrewAI, Dify, and friends

Short version: most tools people compare Primer to are **libraries you import** or **hosted app-builders**. Primer is a **self-hosted platform you run** — an API, a worker pool, and an operator console — for orchestrating fleets of small, context-optimized agents. That's the honest distinction, and it's the one that should decide whether Primer fits.

Three axes matter:

1. **Library vs. platform.** Do you compose primitives into your own service, or do you run a system that already has the operational surface (a console, per-agent isolation, triggers, channels, approvals)?
2. **Context discipline.** Primer's design bet is that a small model with a clean, purpose-built context can rival a much larger one — so it's built for *many small agents*, not one big one.
3. **Knowledge as a live artifact vs. a read-only corpus.** Is your knowledge base something an agent edits as files with a review step back upstream, or a vector index you feed and query?

## At a glance

| | **Primer** | **LangGraph** | **CrewAI / AutoGen** | **Dify / Flowise** | **Temporal / n8n** |
|---|---|---|---|---|---|
| Shape | self-hosted platform | Python library | Python framework | low-code app builder | durable execution / workflow automation |
| You... | run it | import it | import it | host + click | run it (general-purpose) |
| Operator console / UI | ✅ built-in | ~ (hosted/paid tier) | ~ (studio UIs) | ✅ (app builder) | ✅ (ops/automation UI) |
| Per-agent isolation (container/**k8s** workspace) | ✅ | assemble yourself | assemble yourself | ✗ | ~ (workers, not agent sandboxes) |
| MCP server **and** client | ✅ | client-side, DIY | varies | varies | ✗ |
| Park-and-resume (durable wait on human/event, frees compute) | ✅ | interrupts + checkpointing | varies | limited | ✅ (general, not agent-native) |
| Directed cyclic graphs | ✅ | ✅ (this is its core) | ~ | ~ | ✅ (workflows) |
| Human approval gates | ✅ built-in | assemble yourself | assemble yourself | ~ | assemble yourself |
| Channels (Slack/Telegram/Discord) | ✅ built-in | assemble yourself | assemble yourself | ~ | ~ (integrations) |
| Versioned packaging (harnesses) | ✅ | ✗ | ✗ | export/import | workflow versioning |
| Collection ↔ Workspace mount (agent-editable knowledge, 3-way diff + apply-back to source) | ✅ | assemble yourself (pair with your own vector store/RAG) | ~ (knowledge sources for RAG are typically read-only ingestion, not agent-editable with sync-back) | ~ (managed knowledge bases/datasets via UI; not a live-editable workspace mount) | ✗ (not knowledge-oriented) |
| Learning curve | run a server, learn the model | low (it's a lib) | low–medium | lowest (clicks) | medium |

`~` = possible but not first-class / varies by version. This is a map, not a scorecard — several of these are excellent at what they do.

## A newer differentiator: Collection ↔ Workspace mount

Since 0.3.0, Primer lets you mount a whole **Collection** (a path-addressed knowledge base) into an agent's **workspace** as a live, editable directory — from Studio's *Mount collection* action, or at workspace-creation time. The agent reads and writes those files like any other workspace file. When you're ready, a **3-way diff preview + "Apply to collection"** syncs the agent's edits back to the upstream collection (local wins on conflict); **detach** just removes the workspace copy, upstream untouched, with a guard if you have unsynced changes.

The honest framing: none of this is impossible elsewhere — you can always wire a git-backed RAG corpus into a container yourself, or script a sync job. What Primer gives you is that loop *built in*: knowledge as a first-class, versionable, agent-editable artifact, not a read-only vector blob you can only query. It's a small feature so far (it shipped this release), but it's the kind of thing that's awkward to bolt onto a library-shaped tool after the fact, since it needs the workspace and the collection to both already be first-class objects in the system.

## Where each of the others is the better choice

- **LangGraph** — if you want a lightweight, in-process library for stateful agent graphs and you already have (or want to own) your infrastructure. LangGraph is very good at exactly that; Primer is heavier because it brings the server, workers, console, and isolation with it. If you don't want to run a platform, use the library.
- **CrewAI / AutoGen** — if you want a high-level, opinionated way to stand up role-based multi-agent collaboration quickly inside your own app, especially for prototypes and research. Primer is lower-level infrastructure, not a "crew in ten lines."
- **Dify / Flowise** — if you want a hosted, low-code/no-code builder for chatbots and RAG apps with a visual canvas and minimal code. Primer is code-first infrastructure for agent *fleets and loops*, not a drag-and-drop app builder.
- **Temporal / n8n** — if you need general-purpose durable execution or workflow automation that isn't agent-specific. Primer's park-and-resume is the same *idea* (durable waits) but built around agent turns, contexts, and human gates rather than arbitrary workflows.

## When NOT to use Primer (or when it's overkill)

- You're building a **single agent or a prototype** — a script or a library is less to run.
- You want a **fully managed SaaS** — Primer is self-hosted; you operate it (that's the point for some teams, and a cost for others).
- You want **low-code / no engineers** — Primer assumes you're comfortable running a server and Postgres.
- You need **general workflow automation** unrelated to agents — reach for Temporal/n8n.
- It's **v0.3** — early. If you need a long track record today, wait or run a pilot.

## The one claim Primer can make cleanly

Primer is the **self-hostable platform that integrates the operational surface** — console, per-agent container/Kubernetes workspaces, triggers, channels, approvals, versioned harnesses, an MCP server/client, and now a live, agent-editable mount between knowledge collections and workspaces — **around fleets of small, context-optimized agents.** The others give you a piece of that (a graph library, a crew abstraction, a hosted builder, a durable engine); Primer's bet is that having them integrated and self-hostable, with context discipline as the organizing principle, is worth running a platform for.

If you want the library, use the library. If you want to run the fleet, that's what Primer is for.

<!-- editor notes — VERIFY before publishing (competitor products move fast; do not ship claims you can't defend on HN):
1. LangGraph: confirm current console/UI story — LangGraph Studio / LangGraph Platform / LangSmith are separate (some paid/hosted). I framed the OSS library as "no built-in self-hosted console"; double-check that's still fair.
2. CrewAI/AutoGen: both have evolved (CrewAI Enterprise, AutoGen Studio / the ag2 fork). Confirm the "framework/library, not a run-it platform" framing still holds.
3. Dify/Flowise: both are self-hostable and have agent+workflow features now; I framed them as low-code app builders — verify that's still the honest center of gravity vs. Primer's code-first infra angle.
4. Temporal/n8n: included as *mental models* (durable execution / automation), not direct competitors — keep that framing so it doesn't look like a category error.
5. Every "assemble yourself / ✗ / ~" for a competitor: sanity-check you'd defend it. Prefer "Primer includes X; with <tool> you'd wire it up" over "<tool> can't do X."
6. MCP: confirm which competitors now ship first-class MCP server+client vs client-only, since this is moving quickly.
7. Collection ↔ Workspace mount: this is a new (0.3.0) claim — before publishing, double check whether any competitor now offers an equivalent "agent edits knowledge files, diff-and-apply back to source" loop (vs. plain RAG ingestion or a synced folder without a diff/apply step), so the row and the "newer differentiator" section stay defensible.
-->
