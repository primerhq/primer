# Reddit — Primer launch posts

Facts grounded in `docs/marketing/positioning.md` and `README.md`. Five posts,
five different angles, five different opening stories — none of these share a
paragraph. Do not copy-paste between subs; that reads as spam and gets posts
(or accounts) banned.

**Stagger these 2–4 days apart, not the same day.** Suggested order —
most-aligned/forgiving communities first, to bank early signal and feedback
before the toughest audience:

1. r/LocalLLaMA
2. r/selfhosted
3. r/LLMDevs
4. r/Python
5. r/MachineLearning (most rigorous/skeptical — go last, once you've got
   answers to the hard questions from rounds 1–4)

**Before posting to any of these, re-check the sub's current rules/wiki** —
self-promotion policies shift, and some subs require message-the-mods
pre-approval for anything that looks like project promotion. Treat the notes
below as a starting point, not a guarantee of current mod policy.

---

## r/LocalLLaMA

**Self-promo norm:** No strict "10% rule" like some subs, but the community
expects you to disclose you're the creator up front, engage genuinely in
comments (not link-and-leave), and keep the post relevant to local
inference/model use rather than generic startup promotion. Low removal risk if
it's on-topic and not hypey.

**Angle:** self-host, own your stack, small/local models.

**Title:** Built a self-hosted control plane for running fleets of small,
local agents — betting that clean context beats bigger models

**Body:**

> I run local models for most of my agent work, and the thing that kept
> breaking wasn't model quality — it was context. Bigger tasks meant bigger
> prompts: more tool schemas, longer history, more background docs, and even a
> good local model would start losing the thread.
>
> That turned into Primer: an open-source, self-hosted platform (Apache-2.0)
> for orchestrating fleets of small agents instead of one big one. Directed
> graphs for producer/judge loops, workspaces (local/container/k8s) so agents
> get a real persistent filesystem instead of stuffing state into the prompt,
> semantic search so retrieval only pulls the relevant chunks, and a built-in
> MCP server/client so it plugs into whatever you're already running models
> through. New in 0.3.0: you can mount a whole collection of your own docs
> straight into a workspace as an editable directory, so the agent's working
> set is your notes, not a read-only vector blob. Zero-config on SQLite to try
> it, Postgres for real use.
>
> It's 0.3.0 and it's mine — I'm not selling anything, just sharing the thing I
> built to run local agents in a way that doesn't fall over past a toy demo.
> `pipx install 'primer-ai[full]'` or
> `docker run --rm -p 8000:8000 ghcr.io/primerhq/primer:latest`.
>
> Repo: https://github.com/primerhq/primer — happy to talk through the
> local-model angle specifically, that's most of why I built it this way.

---

## r/LLMDevs

**Self-promo norm:** Generally welcomes project shares with real technical
depth. Tag the post appropriately (flair like "Tools" / "Resource" /
"Discussion" if available), disclose you're the creator, and lead with the
engineering problem, not the pitch.

**Angle:** orchestration + graph engineering — the systems-design framing.

**Title:** Graph engineering: designing the structure your agents run inside, not
the prompt itself

**Body:**

> Most agent work I see (and did myself) is still "prompt the agent, read the
> output, prompt again." The more useful abstraction, once you're running
> anything unattended, is the loop: something that wakes on a schedule or
> event, works toward a goal, checks its own output against evidence, and
> escalates to a human only when it should. Framed that way, a loop needs
> specific primitives — a heartbeat, isolation between parallel runs, durable
> memory, a maker and a checker that don't share a context, connectors to real
> tools/people, and a human gate for the risky steps.
>
> I built Primer as the substrate for that: triggers (cron/webhook/delay) for
> the heartbeat, workspaces (local/container/k8s) for isolation, directed
> cyclic graphs for maker/checker loops with fan-out/fan-in, a built-in MCP
> server + client and Slack/Telegram/Discord channels for connectors, and
> approval gates + park-and-resume for the human-in-the-loop part — an agent
> can wait hours on a person without holding compute. Durable memory got more
> concrete in 0.3.0 too: you can mount a whole collection of documents into a
> workspace as a live directory, let the agent edit it like any other file,
> then diff-and-apply those edits back upstream when you trust them.
>
> It's 0.3.0, self-hosted, Apache-2.0. Primer doesn't decide when to press "go"
> on the loop — that's still your design — it just gives you the pieces so
> you're not hand-rolling all of them.
>
> Repo: https://github.com/primerhq/primer. Curious how others here are
> handling the maker/checker split, or if you're doing it in one prompt.

---

## r/selfhosted

**Self-promo norm:** Fairly strict — expects the tool to be genuinely
self-hostable (FOSS strongly preferred; Primer is Apache-2.0), requires
disclosure that you're the developer, and is unforgiving of anything that
reads like a landing page or a pricing pitch. Keep it plain: what it does, how
to run it, what it needs.

**Angle:** run agent fleets on your own infra.

**Title:** Self-hosted control plane for running agent fleets on your own
infra (Docker/Postgres, Apache-2.0)

**Body:**

> Sharing something I've been building: Primer, a self-hosted platform for
> orchestrating fleets of agents on your own hardware — no managed service in
> the loop.
>
> What you get running it yourself: an operator console, per-agent workspaces
> (local, container, or Kubernetes sandboxes, each with a persistent
> git-backed filesystem), directed graphs for multi-step workflows,
> Slack/Telegram/Discord channels so agents can ask/notify/get approved from a
> chat you already use, human-approval gates for anything sensitive, and a
> built-in MCP server so other tools on your network can call into it (plus an
> MCP client so Primer's agents can call out). New this release: you can mount
> a whole collection of your own documents into a workspace as a live,
> editable directory — the agent edits the files directly, and a diff step
> syncs changes back to the collection when you're ready, so your knowledge
> base stays yours, not something handed off to a hosted vector store.
>
> Deploy however you'd deploy anything else self-hosted:
> `docker run --rm -p 8000:8000 ghcr.io/primerhq/primer:latest` for a quick
> look on embedded SQLite, or point it at Postgres (docker-compose file
> included) for multi-worker/production use with pgvector for semantic search.
> `pipx install 'primer-ai[full]'` works too if you'd rather run it bare.
>
> It's 0.3.0, Apache-2.0, source's all up front:
> https://github.com/primerhq/primer. I'm the author — happy to answer
> anything about the k8s workspace backend or the Postgres setup, those are
> the parts I'd want scrutinized most.

---

## r/Python

**Self-promo norm:** Project posts should carry real substance about the
Python engineering, not just a pitch — flair as "Show and Tell" if that option
exists, disclose authorship, and lead with something specific to the
language/ecosystem (architecture, packaging, async, tooling).

**Angle:** FastAPI/architecture, packaging.

**Title:** Primer: an async FastAPI app for orchestrating agent fleets —
Python 3.12, Postgres/pgvector, packaged for pipx

**Body:**

> Sharing a project (flair: Show and Tell) — Primer, an agent-orchestration
> platform, but the part I think is interesting for this sub is the Python
> side of building it.
>
> Stack: Python 3.12, FastAPI + asyncio end to end, SQLite for zero-config
> local runs and Postgres+pgvector for production/semantic search, a worker
> pool that claims sessions/chats/graph runs and drives them turn by turn. The
> operator console is vanilla React (JSX, no build step) served straight off
> the API. Packaging is extras-based: a lean core (`pip install primer-ai`)
> with the REST API, console, MCP server, and API-based LLM/embedder
> providers, and optional extras for the heavier stuff — `[huggingface]`,
> `[docling]`, `[lance]`, `[channels]`, `[docker]`, `[kubernetes]` — or
> `[full]` for all of it, distributed via `pipx` so the CLI stays isolated
> from other environments. There's also a `primectl` CLI and a
> `uv sync --all-extras` path for contributors.
>
> It's 0.3.0, Apache-2.0. If you poke at the code, I'd genuinely like feedback
> on the extras/packaging split — trying to keep the core install lean without
> making `[full]` a black box.
>
> Repo: https://github.com/primerhq/primer

---

## r/MachineLearning

**Self-promo norm:** Use the `[P]` (Project) tag. Heavily research-oriented,
low tolerance for hype or unsubstantiated claims — frame this as a technical
write-up with a clearly stated hypothesis, not a product pitch. Expect the
comments to push back on any claim that isn't measured.

**Angle:** technical project post — the context/attention hypothesis, framed
as a bet worth testing, not a result.

**Title:** [P] Primer — an orchestration platform built to test whether clean
context lets small models substitute for big ones

**Body:**

> Sharing an open-source project rather than a paper, but it's built around a
> specific hypothesis: a model allocates a fixed attention budget across every
> token in its context, so a small, tightly-constructed context should let a
> smaller model perform closer to a larger one on a given task than the same
> small model would with a bloated context (long history, unused tool schemas,
> irrelevant retrieved docs). That's the design bet, not a benchmarked result —
> I don't have controlled numbers for a specific accuracy delta, and I'd treat
> any claim like that skeptically until it's measured properly.
>
> Primer is the platform I built to act on that bet at the level of multi-agent
> systems rather than a single prompt: directed cyclic graphs for
> producer/judge loops and fan-out/fan-in, per-agent workspaces
> (local/container/k8s) so state lives outside the context window, semantic
> search that retrieves only relevant chunks instead of dumping a corpus into
> the prompt, and an MCP server/client for tool exposure. As of 0.3.0 a
> collection of documents can be mounted straight into a workspace as an
> editable directory (with a diff step to sync edits back), which is really
> the same "state outside the context window" idea applied to retrieved
> knowledge instead of scratch files. Stack is Python/FastAPI, Postgres+pgvector
> for the vector store, self-hosted, Apache-2.0.
>
> It's 0.3.0 — genuinely early. I'd be interested in this community's take on
> how you'd actually design an experiment to test the core hypothesis (task
> accuracy vs. context size/composition, holding the model fixed) rather than
> just my anecdotal read from building it.
>
> Repo: https://github.com/primerhq/primer
