# X/Twitter — Primer launch thread

Facts grounded in `docs/marketing/positioning.md` and `README.md`. One idea per
tweet. Thread leads with the demo video (attach it to tweet 1). Keep the
thread's tone terse and mechanism-first — save narrative flourish for
LinkedIn.

## Launch thread (13 tweets)

**1/** (attach the demo video here)
We built Primer around one bet: a small model given a clean, purpose-built
context can rival a much bigger one. Here's a 60-second look at what that
turns into — fleets of small agents, not one giant prompt.

**2/**
The mechanism: a model spreads a fixed attention budget across every token in
its context. Keep it tight and the tokens that matter get the attention.
Bloat it with stale history and unused tool defs, and the signal thins out —
on any model, big or small.

**3/**
So instead of one agent carrying everything, Primer orchestrates fleets of
small, focused agents — each with its own clean working context, wired
together with the primitives a real deployment needs.

**4/**
It's not a library you import into a script. Primer is a self-hosted control
plane you run: a console, an API, a worker pool — the operational surface
libraries don't have.

**5/**
Declarative agent graphs let you wire nodes into producer-judge cycles,
fan-out/fan-in, and conditional branches — multi-step reasoning as structure,
not one sprawling prompt.

**6/**
Workspaces give every agent a real sandbox — local, container, or Kubernetes —
with a persistent, git-backed filesystem. State lives outside the context
window instead of piling up inside it.

**7/**
Agents can park. A slow tool call or a pending human decision doesn't have to
burn compute — the agent yields, frees its resources, and resumes with full
state intact the moment the event fires.

**8/**
New since 0.2: mount a whole Collection into a workspace as a live, editable
directory — the agent reads/writes those files directly. A diff preview +
"Apply to collection" syncs its edits back upstream (local wins on
conflict). Knowledge as files, not a read-only vector blob.

**9/**
Channels bridge agents into Slack, Telegram, and Discord — ask a question,
request an approval, or kick off a run from a message you already read.

**10/**
Sensitive tool calls can sit behind a human approval gate, granted from a
channel or the console. The agent doesn't get to decide everything on its
own.

**11/**
Primer ships a built-in MCP server and an MCP client — expose the whole tool
surface to other agents over MCP, and let Primer's own agents call out to MCP
tools elsewhere.

**12/**
It's 0.3.0. Self-hosted, Apache-2.0, zero-config on SQLite for a first look,
Postgres + pgvector when you need multi-worker or semantic search. Early —
and I want to hear where it breaks for you.

**13/**
Repo, docs, and a 5-minute quickstart: https://github.com/primerhq/primer
If the bet — small model, clean context, real ops surface — resonates, I'd
genuinely like your eyes on it.

---

## Standalone tweets (post independently, not part of the thread)

**A — the core bet, quotable:**
A model doesn't run out of intelligence first. It runs out of attention —
spread thin across every token you hand it. Clean the context and the small
model gets a lot closer to the big one.

**B — graph engineering framing:**
Graph engineering: designing the structure the agents run inside, not the
prompt. A heartbeat, isolation, a maker and a checker, a human gate. The skill
shifts from writing a good prompt to drawing a good graph. A loop is one
shape a graph can take; most real work also branches and fans out.

**C — self-host / ops-surface:**
Console, workspaces, triggers, channels, approvals, harnesses — the
operational surface an agent library doesn't give you. Primer is the control
plane you run yourself, on your own hardware.

**D — MCP angle:**
Most MCP conversations are about consuming servers. Primer also ships one:
the whole platform's tool surface, exposed over MCP, so other agents can
operate Primer directly instead of you scripting around it.
