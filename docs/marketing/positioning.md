# Primer — Positioning & Messaging (source of truth for all copy)

> Everything else in this kit derives its facts + voice from here. Grounded in the README; do not invent capabilities beyond what ships in 0.3.0.

## The core bet (the thing to repeat everywhere)
**A small model given a clean, purpose-built context can rival a much larger one.**
A model spreads a fixed attention budget across every token. Keep the context tight and the tokens that matter get the attention; bloat it and the signal thins. Context is a lever on *any* model. Primer is the platform for exploiting that lever at fleet scale.

## Category reframe (get out of the "agent framework" knife-fight)
- Not a library you `import` into a notebook → a **self-hosted control plane you run**.
- Mental model: *the ops/control layer for fleets of small agents* (the "Temporal/Kubernetes for agent fleets" analogy — use loosely, don't overclaim).
- What makes it a platform, not a lib: an operator **console**, **workspaces** (local/container/k8s), **channels**, **triggers**, **approvals**, **harnesses** — the operational surface libraries don't have.

## One-liner options (pick one; A/B on channels)
1. *The self-hosted control plane for fleets of small, context-optimized agents.*
2. *Run fleets of small agents in production — a clean context each, wired with graphs, workspaces, triggers, and channels. Self-hosted, MCP-native.*
3. *An agent-orchestration platform built on one bet: a small model with a clean context can rival a much bigger one.* (closest to current README — safest)
4. *The orchestration substrate for loop engineering.* (narrative-forward; pairs with the manifesto)

**Repo description (≤120 chars):** *Self-hosted control plane for fleets of small, context-optimized agents: graphs, workspaces, channels, triggers, MCP.*

## Four proof pillars (every asset leans on ≥1)
1. **Control plane, not a library** — console, workspaces, ops surface; you *run* it.
2. **MCP-native** — built-in MCP server *and* client; operate Primer *with* agents. (freshest, least-crowded hook)
3. **Loop engineering** — designing the system that prompts the agent, not the prompt. The philosophy Primer is built for.
4. **Batteries-included & self-hostable** — graphs, semantic search, channels, triggers, approvals, harnesses — integrated, Apache-2.0, on your hardware.

## Dual-ICP messaging matrix
| | **Indie AI builders** | **Platform / infra teams** |
|---|---|---|
| Their pain | agent scripts are spaghetti; no UI, no ops, context bloat kills accuracy | need agents in prod with control, isolation, observability, human gates |
| Lead pillar | MCP-native + batteries-included + a real console | control plane + isolation (k8s workspaces) + approvals/audit |
| Proof points | `pipx install`, console at `/console`, park-and-resume, meta-tool discovery, collection↔workspace mount (your notes become the agent's editable working set) | Postgres, per-agent k8s sandboxes, triggers/webhooks, approval gates, harnesses, collection↔workspace mount (curated knowledge round-trips through a controlled workspace with a diff/review step) |
| Tone | builder-to-builder, show don't tell | ops/production credibility, restraint |
| Channels | Reddit, HN, X, dev.to | LinkedIn, comparison content, docs depth |

## Concrete capabilities (only claim these — all ship in 0.3.0)
- **Yielding / park-and-resume agents** — park on a slow tool or human decision, free compute, resume when the event fires.
- **Directed cyclic graphs** — producer-judge loops, fan-out/fan-in, conditional branches, runtime agent/graph invocation.
- **Workspaces & sessions** — per-agent local / container / **Kubernetes** sandbox; persistent, git-backed filesystem + state.
- **Semantic search** — ingest → vector collections; retrieve only relevant chunks.
- **Collection ↔ workspace mount** — mount a collection into an agent's workspace as a live, editable directory (from Studio's *Mount collection* or at workspace-creation time); agents read/write those files directly. A 3-way diff + **Apply to collection** syncs local edits back upstream (local-wins on conflict); detach removes the workspace copy only, with an unsynced-changes guard.
- **Channels** — Slack / Telegram / Discord: ask, approve, trigger from a message.
- **MCP server + client** — expose the full tool surface over MCP; also consume MCP. Creating a toolset now probes the endpoint first and blocks unreachable ones (with a "Create anyway" override).
- **Human approvals** — gate sensitive tool calls from a channel or the console.
- **Harnesses** — package agents/graphs/collections into a versioned, git-backed bundle; deploy anywhere.
- **Dynamic discovery** — two meta-tools let an agent find + invoke any tool/agent at runtime without carrying the catalog in-context.
- **Stack:** Python 3.12, FastAPI, SQLite (zero-config) / Postgres+pgvector (prod), operator console, `primectl` CLI, Docker/GHCR images.

## Honesty guardrails (protects credibility on HN/Reddit)
- It's **0.3.0** — say "early / v0.3" where relevant; don't say "production-ready" flatly.
- Primer **doesn't press "go"** on the loop for you — it's the substrate; a human stays in it where it matters.
- "Rival a much larger one" is a **bet/thesis**, framed as such — not a benchmarked guarantee.
- No invented benchmarks, users, or logos.

## Voice
Technical, specific, understated. The audience (HN/Reddit/AI-eng) rewards precision and punishes marketing gloss. Short sentences. Concrete nouns. Show the mechanism. No "revolutionary/seamless/powerful/unlock/game-changing."

## Canonical links
- Repo: https://github.com/primerhq/primer · PyPI: `primer-ai` · Docs: https://primerhq.github.io · Console: `/console/`
- Install: `pipx install 'primer-ai[full]'` · `docker run --rm -p 8000:8000 ghcr.io/primerhq/primer:latest`
