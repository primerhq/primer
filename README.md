<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/primerhq/primer/main/docs/assets/hero-dark.png">
  <img alt="Primer - orchestrate fleets of small, context-optimized agents" src="https://raw.githubusercontent.com/primerhq/primer/main/docs/assets/hero-light.png" width="760">
</picture>

<br>

**A self-hosted, open-source platform for orchestrating fleets of small, context-optimized AI agents - built on one bet: a small, local open-weight model, given a clean and purpose-built context, can do genuinely useful work. Runs on hardware you already own.**

<br>

[![License](https://img.shields.io/badge/license-Apache_2.0-61d46a.svg)](https://github.com/primerhq/primer/blob/main/LICENSE)
[![Release](https://img.shields.io/github/actions/workflow/status/primerhq/primer/release.yml?branch=main&label=release)](https://github.com/primerhq/primer/actions/workflows/release.yml)
[![Python](https://img.shields.io/badge/python-3.12+-3776ab.svg)](https://www.python.org/)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-61d46a.svg)](https://github.com/primerhq/primer/blob/main/CONTRIBUTING.md)
[![Stars](https://img.shields.io/github/stars/primerhq/primer?style=flat&color=61d46a)](https://github.com/primerhq/primer/stargazers)

[Quickstart](#quickstart) · [What makes it different](#what-makes-primer-different) · [Loop engineering](#built-for-loop-engineering) · [How it works](#how-it-works) · [Docs](#documentation) · [Contributing](https://github.com/primerhq/primer/blob/main/CONTRIBUTING.md)

</div>

---

## Why Primer

A language model spreads a fixed budget of attention across every token in its context. Keep that context tight and the few tokens that matter get most of the attention; bloat it with stale history, unused tool definitions, and irrelevant background, and the signal thins out. Primer's bet is simple: **give a small, local model exactly what it needs - and nothing more - and it can do genuinely useful work.** Not replace a frontier model; just real work, on hardware you already own. It is a bet, not a benchmark, and it is still early - the best way to test it is to run it on your own workload and tell us where it falls apart.

So instead of one giant agent with everything crammed into its prompt, Primer lets you orchestrate **fleets of small, focused agents**, each with a clean working context, wired together with the primitives a real deployment needs: LLM providers, workspaces, agent graphs, knowledge collections, channels, triggers, and semantic search - self-hosted and integrated from the start.

## What makes Primer different

A lot of what Primer ships - knowledge bases, channels, triggers, approvals - you will find in other agent frameworks too. These are the parts that were missing everywhere else, and they are what Primer is really about.

**🔁 Directed cyclic agent graphs.** Wire small agents into a graph that *loops*. Run a bunch of small agents in a feedback loop and you are trading compute for time: the loop keeps running until it reaches the state you want. The move that makes it click is putting an evaluator agent at the end that grades the output and feeds it back to the start - a producer makes a draft, a critic scores it, the graph revises, again and again, until the result is actually good. Instead of a one-shot prompt you hope lands, you get a loop that **converges on a target**.

**📁 Shared workspaces.** Run multiple agents and graphs inside a single sandbox, all reading and writing the same filesystem. They work independently but share everything they find - one agent writes a file, another picks it up. It is the simplest possible way to let a fleet of agents collaborate on the same task.

**⏸️ Yielding tools (event-driven, long-running agents).** Loops and graphs are meant to run in the background - you should not have to sit in front of a screen keeping a session open. So an agent can call a tool that **yields control and parks the agent** until an event fires: a file change, a schedule, a webhook, or a human reply. That is what makes long-running, event-driven agents possible - and combined with shared workspaces, one agent can wake the instant another writes to a file.

**🔎 Semantic tool search.** Register tens of tools on an agent and the definitions alone bloat its context. Instead, Primer embeds every tool as a vector and hands each agent just two meta-tools: one to **search** for the capability it needs, and one to **call** any tool in the platform. Two tools in context, access to all of them. It generalizes - an agent can discover and invoke any other agent or graph the same way.

**🧩 First-class dogfooding.** The platform's own capabilities are exposed as internal tools, so you can build **agents that build other agents**, graphs, and collections - on Primer itself.

**🔌 MCP over the whole platform.** Every capability is exposed over the **Model Context Protocol**. Point Claude, opencode, or any MCP client at Primer and **operate it by asking an agent** instead of clicking through a UI. You drive the platform with agents, not just point agents at tools.

## Batteries included

Everything else a real deployment needs, integrated from day one and self-hostable:

- **Knowledge collections** - ingest documents into vector collections; agents retrieve only the relevant chunks (semantic search / RAG).
- **Channels** - bridge agents to **Slack, Telegram, and Discord**: ask questions, request approvals, and kick off work from a message.
- **Triggers** - start a fresh session or graph run, or resume a parked one, on a **cron schedule, a delay, or a webhook**.
- **Human approvals** - gate sensitive tool calls behind a person's approval from a channel or the console before the agent proceeds.
- **Web search** - first-class web search built in.
- **MCP-server toolsets** - connect external MCP servers and expose their tools to your agents.
- **Harnesses** - package a tuned set of agents, graphs, and collections into a versioned, git-backed bundle you can share and deploy anywhere.

<!-- DEMO GIF: drop the ask_user -> channel reply -> resume capture here once recorded, e.g.
<div align="center"><img alt="Park on a question, reply from a channel, resume" src="docs/assets/demo-park-resume.gif" width="760"></div>
-->

<!-- SCREENSHOTS: add a framed-console section here (Dashboard / Session control room / Approvals / Graph editor) once the captures are produced. -->

## Built for loop engineering

Loop engineering is the shift from prompting an agent turn-by-turn to **designing the system that prompts it** - a loop that wakes on a schedule, works toward a stated goal, checks its own output against evidence, and escalates to a human only when it should. The leverage moves from writing a good prompt to designing a good loop.

A loop needs a specific set of primitives. Primer ships all of them, integrated and self-hostable:

| A loop needs... | Primer gives you |
|---|---|
| **A heartbeat** - work surfaced on a cadence, not by hand | **Triggers** that start a fresh session or graph run (or resume a parked one) on a cron schedule, a delay, or a webhook |
| **Isolation** - parallel agents that don't collide | **Workspaces** - a per-agent local, container, or Kubernetes sandbox with its own persistent, git-backed filesystem |
| **Durable memory** - the agent forgets, the repo doesn't | Git-backed workspace **state** plus **knowledge collections** agents retrieve from, so knowledge compounds across runs instead of resetting to zero |
| **A maker and a checker** - keep the writer away from the grader | **Directed cyclic graphs** with producer-judge loops, fan-out/fan-in, and runtime agent/graph invocation |
| **Connectors** - reach real tools and real people | A built-in **MCP server** (and MCP client), plus **Slack / Telegram / Discord** channels |
| **A human gate** - approve the risky, let the safe run | **Approval gates** and **park-and-resume**: an agent waits on a person for hours without holding compute, then continues when the reply lands |

Primer does not press "go" on the loop for you - it gives you the orchestration substrate to build one and to keep a human in it where that matters. And the same context discipline that makes a single agent accurate is what lets a loop run for a long time without drifting: each iteration gets a clean, purpose-built context instead of an ever-growing transcript.

## Quickstart

Pick whichever install fits. All three start the same server zero-config on an embedded SQLite database - perfect for a first look.

**pipx** (isolated CLI install; needs Python 3.12+):

```bash
pipx install 'primer-ai[full]'                   # batteries-included
primer api                                       # API + in-process worker
```

The bare `pipx install primer-ai` installs a lean core (REST API, console, MCP, SQLite/Postgres storage, and the API-based LLM/embedder providers). The `[full]` extra adds the optional backends - local HuggingFace embeddings, Docling ingestion, LanceDB, Slack/Telegram/Discord channels, and the container/Kubernetes workspace backends - which pull a larger ML stack. You can also pick à la carte: `primer-ai[huggingface]`, `[docling]`, `[lance]`, `[channels]`, `[docker]`, `[kubernetes]`.

**Docker** (no Python toolchain required):

```bash
docker run --rm -p 8000:8000 ghcr.io/primerhq/primer:latest
```

**From source** (for contributors):

```bash
git clone https://github.com/primerhq/primer.git
cd primer
uv sync --all-extras
uv run primer api
```

Then verify and open the console:

```bash
curl http://localhost:8000/v1/health             # -> {"status":"ok"}
```

The operator console is at **http://localhost:8000/console/**.

### Going to Postgres (multi-process, semantic search, production)

Zero-config SQLite is single-process and ships without a vector store. For multiple workers, semantic search, or production, point Primer at Postgres:

```bash
docker compose up -d postgres                    # or: podman compose up -d postgres
cp config.example.yaml config.yaml               # set db.config.password to match
uv run primer api --config config.yaml
```

`config.example.yaml` documents every field. Environment variables override file values: every `AppConfig` field maps to `PRIMER_<FIELD>` (nested fields use `__`, e.g. `PRIMER_DB__CONFIG__PASSWORD`). The Docker image reads the same variables - set `PRIMER_DB_HOST` (and friends) and it renders a Postgres + pgvector config automatically; otherwise it runs the embedded-SQLite path above. For a SQLite database that survives container restarts, mount a volume at `/app/data`.

## How it works

Primer is a stack of layers, where each layer keeps the one below it from getting cluttered:

- **Context discipline** - tool selection, meta-tools, and internal collections keep each agent's prompt lean.
- **State** - workspaces give agents a shared, minimal surface to hand off results without carrying history in-context.
- **Sequencing** - directed cyclic graphs express multi-step reasoning as structure instead of one giant prompt.
- **Time** - event-driven park-and-resume frees compute while work waits on a slow tool or a human.
- **Sharing** - harnesses package a working configuration into a versioned, git-backed bundle.
- **Edges** - channels, web search, and approval gates handle where agents reach outside the platform.

At runtime, requests arrive from many edges (REST/console, MCP clients, chat channels, triggers), become **sessions / chats / graph runs** that a worker pool claims and drives; each turn calls LLM providers, tools, workspaces, and collections, and can park on a human or event and resume later - all backed by Postgres.

## Documentation

- **Operator docs** - served at `/docs` when the server is running.
- **Agent-usage docs** - [`docs/agents/`](https://github.com/primerhq/primer/tree/main/docs/agents) - how to drive a running Primer instance from an AI agent over MCP.
- **Developer docs** - [`docs/dev/`](https://github.com/primerhq/primer/tree/main/docs/dev) - architecture patterns and subsystem references. Start at [`docs/dev/README.md`](https://github.com/primerhq/primer/blob/main/docs/dev/README.md).

## Contributing

Read [AGENTS.md](https://github.com/primerhq/primer/blob/main/AGENTS.md) first - it is the authoritative contributor contract (project layout, the Definition of Done, how to run the suites, and the hard rules). [CONTRIBUTING.md](https://github.com/primerhq/primer/blob/main/CONTRIBUTING.md) is the human-facing summary.

```bash
uv sync --all-extras
docker compose up -d postgres
# narrowed unit sweep (excludes e2e/distributed/ui_e2e):
uv run pytest tests/ -q --ignore=tests/distributed --ignore=tests/ui_e2e \
  --ignore=tests/e2e --ignore=tests/integration --ignore=tests/llm
```

See [`CODE_OF_CONDUCT.md`](https://github.com/primerhq/primer/blob/main/CODE_OF_CONDUCT.md) for community expectations.

## Security

Please report vulnerabilities privately - see [SECURITY.md](https://github.com/primerhq/primer/blob/main/SECURITY.md).

## License

Primer is licensed under the Apache License 2.0. See [LICENSE](https://github.com/primerhq/primer/blob/main/LICENSE) for the full text.
