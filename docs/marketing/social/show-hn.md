# Show HN — Primer

Facts grounded in `docs/marketing/positioning.md` and `README.md`. Do not add
claims beyond what's there. This file is a draft for founder review before
posting — nothing here gets submitted automatically.

## Title options (pick one)

1. `Show HN: Primer – a self-hosted control plane for fleets of small agents`
2. `Show HN: Primer – MCP-native orchestration for fleets of small, context-optimized agents`
3. `Show HN: Primer – graphs, workspaces, and channels for small-model agent fleets`

Each leans on a different proof pillar (control plane / MCP-native / batteries-included
primitives) — pick based on which crowd you expect that day, or A/B across a
resubmission if the first attempt doesn't get traction.

## Post body

What to put in the text field when submitting (or use as the URL's accompanying
context if HN only takes one):

> Primer is a self-hosted platform for running fleets of small agents, each with
> a clean, purpose-built context instead of one big agent carrying a bloated
> prompt. It ships the operational pieces a real deployment needs out of the
> box: a console, workspaces (local, container, or Kubernetes), directed graphs
> for producer-judge loops, Slack/Telegram/Discord channels, human approval
> gates, and a built-in MCP server and client. Since 0.2, you can also mount a
> whole collection of documents into a workspace as a live, editable directory
> and sync an agent's edits back with a diff step. It's 0.3.0, Apache-2.0, and
> installs with `pipx install 'primer-ai[full]'` or
> `docker run --rm -p 8000:8000 ghcr.io/primerhq/primer:latest` on zero-config
> SQLite for a first look.
>
> Repo: https://github.com/primerhq/primer
> Docs: https://primerhq.github.io

## First comment (post this yourself, immediately, as the OP)

> Hi HN, I'm the author.
>
> The problem I kept hitting building agents: give a model more context so
> it's reliable across more cases, and the same model that was sharp on a
> narrow task gets vague once the prompt fills up with tool schemas, chat
> history, and half-relevant background. The bet behind Primer is that a small
> model given a clean, purpose-built context can rival a much larger one — a
> model spreads a fixed attention budget across every token, so a tight context
> gets more of that budget on the tokens that actually matter. I want to be
> upfront that this is a thesis I'm building around, not a benchmarked
> guarantee — I don't have a chart to show you a specific multiplier.
>
> Primer is the platform for acting on that bet at fleet scale: a console,
> workspaces (local/container/k8s), directed graphs for producer-judge loops
> and fan-out/fan-in, semantic search, Slack/Telegram/Discord channels, human
> approval gates, park-and-resume for agents waiting on something slow, and an
> MCP server plus client to drive Primer from other agents, not just point it
> at them. Since 0.2: mount a Collection into a workspace as a live, editable
> directory, then diff-and-apply an agent's edits back upstream — knowledge as
> files, not a read-only vector blob.
>
> It's 0.3.0 — self-hosted, Apache-2.0, and the golden path (the SQLite
> quickstart) is solid, but I'd expect rougher edges the further you get from
> it. Tell me where it breaks for your use case. I'll be around all day.

(~247 words — trim if it drifts past 250.)

## Launch-day checklist

- [ ] Post Tuesday–Thursday, ~8–10am ET (peak HN traffic; avoid weekend lull
      and Monday/Friday drop-off).
- [ ] Block out 8+ hours to actually reply — most of the judgment on Show HN
      happens in the comments, not the post itself.
- [ ] Submit the first comment yourself immediately after the post goes live —
      it anchors the thread before anyone else's take does.
- [ ] Don't ask for upvotes anywhere (HN guidelines violation, gets flagged
      fast, and reads as exactly the kind of hype this audience punishes).
- [ ] Have someone else run the quickstart on a clean machine right before you
      post — the first replies will test it within minutes.
- [ ] Sequence this last, per the launch plan: after the Reddit rounds have
      already surfaced and fixed first-impression issues, not before.
