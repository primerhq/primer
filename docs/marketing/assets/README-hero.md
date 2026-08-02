# README hero — sharpen options

The current hero is already good. These are small, high-leverage tweaks, not a rewrite. The README converts HN/Reddit traffic into stars, so the top 5 lines and the first visual matter most.

## Tagline options (the bold line under the logo)
Current: *"An unopinionated, batteries-included agent-orchestration platform built around one bet: a small model given a clean, purpose-built context can rival a much larger one."* — strong; keep as the safe default.

Sharper alternates to A/B:
- **A (control-plane reframe):** *"The self-hosted control plane for fleets of small, context-optimized agents. One bet: a small model with a clean context can rival a much larger one."*
- **B (loop-forward):** *"Batteries-included orchestration for loop engineering — fleets of small agents, each with a clean context, wired with graphs, workspaces, triggers, and channels. Self-hosted, MCP-native."*
- **C (tightest):** *"Run fleets of small, context-optimized agents in production. Self-hosted. MCP-native. Batteries included."*

## New for 0.3.0: add a hero bullet for Collection ↔ Workspace mount

This is the headline addition since 0.2.0, and it belongs in the hero, not buried in the changelog. **Collection ↔ Workspace mount**: mount a whole knowledge Collection into an agent's workspace as a live, editable directory (from Studio's *Mount collection*, or at workspace-creation time) — the agent reads and writes those files like any other workspace file. A 3-way diff + **Apply to collection** syncs its edits back upstream (local wins on conflict); **detach** drops the workspace copy only, with an unsynced-changes guard.

Suggested card copy, matching the existing "What you can build" grid's emoji + bold + one-liner style:

> 🔗📁 **Collection ↔ Workspace mount** — Mount a knowledge collection as a live, editable directory inside a workspace; agents read and write the files directly, then sync edits back upstream with a diff-and-apply step.

Add it as a 7th card in that grid (or fold it into the existing "Workspaces & sessions" card if you'd rather not grow it) — it reinforces both existing pillars: *state lives outside the context window* and *own your stack*. Knowledge here is a first-class, versionable, agent-editable artifact, not a read-only vector blob — worth surfacing in the tagline-adjacent bullets and in the repo Topics/description too.

Optional, only if a feature list has room: a light one-liner on the **toolset connectivity guard** — creating an MCP toolset now probes the endpoint first and blocks unreachable ones (with a "Create anyway" escape hatch). Real, but small — a footnote on the MCP server card, not a headline.

## Structural tweaks (in priority order)
1. **Put the demo GIF directly under the tagline** (it's currently commented out at line ~106). The first visual should be motion, not badges. This is the single biggest README improvement — do it the moment the capture exists.
2. **Add 2–3 framed console screenshots** (graph run view, approvals, agents) in the "What you can build" area — the console is your differentiator; show it.
3. **Add a one-line "▶ 60-second demo" link** near the top pointing at the MP4/YouTube, for people who won't watch an inline GIF.
4. Keep the badges, but move them *below* the tagline+visual (they're social proof, not the hook).
5. Consider surfacing **"MCP-native"** in the first screen — it's the freshest hook and a search term.

## Repo "About" (right sidebar) — apply via Settings or `gh repo edit`
- **Description (≤120 chars):** `Self-hosted control plane for fleets of small, context-optimized agents: graphs, workspaces, channels, triggers, MCP.`
- **Website:** `https://primerhq.github.io`
- **Topics** (current set is good; consider adding): `mcp-server`, `agent-framework`, `self-hosted`, `llmops`, `agentic`

> To apply the description without touching the UI: `gh repo edit primerhq/primer --description "<above>"` — say the word and I'll run it (I have admin).
