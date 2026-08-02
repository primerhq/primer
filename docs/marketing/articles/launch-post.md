<!--
ICP tailoring note for the editor — swap the opening and the first pillar to match the audience:

- Indie AI builders (Reddit / HN / X / dev.to): lead with the console, `pipx install`, park-and-resume,
  and meta-tool discovery. Tone: builder-to-builder, show don't tell. Open on "your agent script is
  spaghetti, has no UI, and its context keeps bloating until accuracy drops."

- Platform / infra teams (LinkedIn / comparison content / docs): lead with the control-plane framing,
  Kubernetes workspace isolation, approval gates + audit trail, triggers/webhooks, and the Postgres path.
  Tone: production restraint. Open on "you need agents in prod with isolation, human gates, and an ops surface."

Keep the bet, the walkthrough, and the install block for both. Only the first paragraph and the first
bullet under "What Primer is" need to change.
-->

# We built a control plane for agent fleets

Three problems show up the moment you try to run an agent in production instead of in a notebook.

**Context bloat degrades accuracy.** A model spreads a fixed budget of attention across every token in its context at once. Cram in stale history, every tool definition it might need, and a pile of background, and the tokens that actually matter get a thinner slice of that budget. The output gets worse — and it gets worse quietly, without an error.

**Agent scripts don't survive contact with production.** The loop that worked on your laptop has no heartbeat, no isolation between parallel runs, no way to wait on a human without holding a process open, and nothing to resume from after a crash. It's a prototype with a `while` loop.

**There's no ops surface.** While it runs, you can't watch the sessions, act on a risky action, or see what happened. You have logs, if you remembered to add them.

## The bet

Primer starts from one bet: a small model given a clean, purpose-built context can rival a much larger one. A model has a fixed attention budget; spend it on the few tokens that matter and a small model punches above its size. That's a thesis, not a benchmark — but it points the whole design one way. Instead of one giant agent with everything crammed into its prompt, run fleets of small, focused agents, each with a clean context, wired together with the primitives a real deployment needs.

## What Primer is

Primer is a self-hosted control plane for those fleets. Not a library you import into a script — a server you run, with an operator console, a REST API, and an MCP interface.

- **Console** — a UI to watch sessions, graph runs, and chats, and to act on approvals as they come up.
- **Workspaces** — each agent gets a sandbox (local, container, or Kubernetes) with a persistent, git-backed filesystem and state.
- **Graphs** — wire agents into directed cyclic graphs: producer-judge loops, fan-out/fan-in, conditional branches, runtime agent invocation.
- **Triggers** — start a session or graph run, or resume a parked one, on a cron schedule, a delay, or a webhook.
- **Channels** — bridge agents to Slack, Telegram, and Discord, so they can ask, request approval, and be triggered from a message.
- **Approvals** — gate a sensitive tool call behind a human decision made from a channel or the console.

It's MCP-native in both directions: Primer exposes its full tool surface over MCP so external agents can drive it, and it consumes MCP tools itself.

## One loop, concretely

Here is the kind of thing the parts add up to.

You have an agent that triages incoming support issues and, when it decides one warrants a refund, has to get a person's sign-off. You don't want it holding a compute slot while it waits, and you don't want anyone babysitting a terminal.

In Primer: a webhook trigger wakes the agent on a new issue. It works in its own workspace, pulls the relevant policy from a knowledge collection — retrieved on demand, not pasted into a giant system prompt — and drafts a decision. The refund step sits behind an approval gate, so the agent posts the request to a Slack channel and parks. It yields its compute and waits. Hours later, someone replies "approve" in Slack. The reply lands, the parked agent resumes from where it stopped, executes the refund, and closes the issue out. No held process, no polling loop, and every step is visible in the console.

That's park-and-resume, channels, approvals, a workspace, and a trigger doing one job together — each a thing you'd otherwise build and maintain yourself.

## What's new in 0.3.0

The workspaces above just got more useful for one specific case: your knowledge base. You can now mount a whole collection into an agent's workspace as a live, editable directory — from Studio's **Mount collection** action, or at workspace-creation time. The agent doesn't just retrieve snippets from it anymore; it reads and edits those files like any other file in its sandbox.

When it's done, a 3-way diff shows what changed, and **Apply to collection** pushes the edits back upstream (local wins on conflict). **Detach** drops the workspace copy without touching the collection, and warns you first if there are unsynced changes. The point: your knowledge stops being a read-only blob the agent queries and becomes something it can actually work on, with a review step before anything upstream changes.

Smaller, but worth knowing if you run this in production: creating an MCP toolset now checks that the endpoint actually answers before letting you save it, instead of leaving a silently broken entry in your tools catalogue. There's a "Create anyway" override if you need one.

## Install

All three paths start the same server zero-config on embedded SQLite — enough for a first look.

```bash
pipx install 'primer-ai[full]'     # batteries-included
primer api                          # API + in-process worker
```

Or Docker, no Python toolchain required:

```bash
docker run --rm -p 8000:8000 ghcr.io/primerhq/primer:latest
```

Then open the console at **http://localhost:8000/console/**. For multiple workers, semantic search, or production, point Primer at Postgres + pgvector; `config.example.yaml` documents every field.

## What's early, what's next

Primer is v0.3. The primitives above ship and work, but it is early software — expect rough edges, changing APIs, and gaps in the docs. It does not press "go" on your loop for you; by design, a human stays in it where that matters. And "rival a much larger one" is the bet we're building on, framed as a bet, not a number we're claiming.

If you're kicking the tires, the console and `pipx install 'primer-ai[full]'` are the fastest way in. If you're evaluating it for a platform, the Kubernetes workspace backend, the approval gates, and the Postgres path are where to look.

## Try it

- Install it and open the console: `pipx install 'primer-ai[full]'`.
- Star the repo if the direction resonates: **https://github.com/primerhq/primer**
- Tell us where it breaks or what's missing — open a thread in GitHub Discussions.

It's Apache-2.0 and self-hosted. Bring your own models, run it on your own hardware, and see whether a clean context beats a bigger one for your workload.

<!-- editor notes: (1) The refund-triage walkthrough is an illustrative composition of real primitives (trigger + workspace + collection + approval gate + channel + park-and-resume), not a shipped demo or a canned tutorial — reword if you'd rather point at an example that actually exists in the repo. (2) "resumes from where it stopped" reflects park-and-resume; confirm the wording matches the actual resume semantics (session continues vs. fresh session picking up the reply). (3) "Console — watch sessions, graph runs, and chats, and act on approvals": approvals-from-console and session/graph/chat views are grounded; I dropped an explicit "inspect a workspace from the console" claim I couldn't confirm — add it back if the console does expose that. (4) CTA says "GitHub Discussions" per the brief — verify Discussions is enabled on primerhq/primer, or swap to Issues. (5) Nothing here calls v0.3 "production-ready"; "for production, point Primer at Postgres" mirrors the README's own phrasing. -->
