# LinkedIn — Primer launch posts

Facts grounded in `docs/marketing/positioning.md` and `README.md`. LinkedIn
voice: slightly more narrative than HN/Reddit, still no hype, still no
"production-ready" absolutes for a 0.3.0 project. Two posts, two ICPs — post
them a few days apart, not back to back.

---

## Post (a) — indie-builder narrative

Lead pillar: the core bet + one concrete capability (park-and-resume). Ends
with an invitation, not a pitch.

> Every agent I built eventually hit the same wall: it worked fine in a demo,
> then got vague and unreliable the moment the task needed more context. It
> was the same problem in a different outfit each time — more tool
> definitions, longer conversation history, more retrieved background — and
> the model's attention thinned across all of it.
>
> The bet I'm building around: a small model given a clean, purpose-built
> context can rival a much bigger one. Attention is a fixed budget per token;
> a tight context puts more of it where it matters.
>
> One piece of Primer that comes directly out of that bet: park-and-resume. An
> agent can park on a slow tool call or a pending human decision and free its
> compute entirely, then pick back up — with its full state intact — the
> moment the event fires. No agent sitting in a loop burning tokens waiting for
> someone to answer a message.
>
> Primer is the open-source, self-hosted platform I built around this: graphs,
> workspaces, channels, approvals, an MCP server and client. It's 0.3.0 —
> early, and I'm looking for people willing to poke holes in it.
>
> Repo: https://github.com/primerhq/primer

---

## Post (b) — platform-team ops framing

Lead pillar: control plane + isolation + approvals + production posture,
honestly hedged for 0.3.0.

> If you're running agents anywhere near production, the framework question
> stops being "which library" and becomes "what's the operational surface."
> Isolation between concurrent runs. An audit trail. A human gate on the tool
> calls that matter. Somewhere to actually see what a fleet of agents is doing
> right now, not just tail logs.
>
> That's the gap Primer is built to close: it's a self-hosted control plane,
> not a library you import into a script. Each agent gets its own workspace —
> a local, container, or Kubernetes sandbox with a persistent, git-backed
> filesystem — so parallel runs don't collide. Approval gates sit in front of
> sensitive tool calls and can be granted from Slack/Telegram/Discord or the
> console. Harnesses package a working set of agents, graphs, and collections
> into a versioned, git-backed bundle you can deploy the same way twice.
> Everything runs on Postgres in production, SQLite for a fast first look.
>
> Two smaller things in 0.3.0 that read as ops posture rather than features:
> curated knowledge can now be mounted straight into a workspace — a
> Collection becomes a live, editable directory an agent works against, and a
> diff preview plus "Apply to collection" step is the review gate before those
> edits sync back upstream, so it stays a controlled, versioned artifact
> instead of a black-box embedding index. And creating an MCP toolset now
> probes the endpoint first and blocks ones that aren't reachable, instead of
> letting a misconfigured toolset sit quietly broken in your catalogue.
>
> It's 0.3.0 — the primitives are there and the golden paths (the SQLite
> quickstart, the docker-compose Postgres setup) are solid, but I'd call this
> early rather than battle-tested at scale. If your team is evaluating agent
> infrastructure and wants to compare notes on what a real deployment needs,
> I'd welcome the conversation.
>
> Repo: https://github.com/primerhq/primer
