# Shared brief: refresh the launch kit for 0.3.0

> Read this before editing any launch-kit file. It has the product framing, the 0.3.0 delta to weave in, the voice rules, and the version/copy rules. You are REFRESHING existing drafts (preserve their structure + good copy), not rewriting from scratch.

## What Primer is (the framing — keep it consistent)
- **The bet:** a small model given a clean, purpose-built context can rival a much larger one — because every transformer spreads a fixed unit of attention across all tokens, so a tight context puts more of it on the tokens that matter (helps big models too). This is a **thesis, not a benchmarked result** — say so; that candor is the credibility.
- **Category:** NOT a framework you `import` — a **self-hosted control plane you run** for fleets of small, context-optimized agents. Python 3.12 / FastAPI; operator console at `/console`; Apache-2.0.
- **Ownable narrative:** **graph engineering.**
- **Freshest hook:** **MCP-native** (built-in MCP server *and* client — drive Primer *with* agents).
- **Origin story:** wanted to run useful agents on a 16GB gaming GPU (RTX 5060 Ti), which caps you ~a 12B model at 4-bit → the whole design follows from that constraint.
- **Two ICPs:** indie AI builders (speed, self-host, MCP, a real console) · platform/infra teams (control plane, isolation, ops, production posture).
- **Existing pillars (already in the drafts):** park-and-resume agents; directed cyclic graphs (producer/judge loops); workspaces (local/container/**k8s** sandbox, git-backed fs — state lives *outside* the context window); channels + approvals (Slack/Telegram/Discord); harnesses (versioned bundles).

## The 0.3.0 delta to weave in (this is what's NEW since the 0.2.0-era drafts)
**1. Collection ↔ Workspace mount (the headline — a real differentiator).**
- Mount a whole **Collection** (a knowledge base of path-addressed documents) into an agent's **workspace** as a **live, editable directory** — from Studio (*Mount collection*) or at workspace-creation time.
- Agents **read/write/update** those files like any workspace files.
- A **3-way diff preview + "Apply to collection"** syncs the agent's local edits **back to the upstream collection** (local-wins on conflict); **detach** removes the workspace copy only (upstream untouched) with a "you have unsynced changes" guard.
- **The angle:** knowledge is a **first-class, versionable, agent-editable artifact** living as files the agent works on — not a read-only vector blob. Reinforces two existing pillars: *state outside the context window* (workspaces) and *own your stack*. Good for BOTH ICPs (indie: "your notes become the agent's editable working set"; platform: "curated knowledge round-trips through a controlled workspace with a review/diff step").
- Where it belongs: README hero (a bullet), launch post (a "what's new in 0.3.0" beat under workspaces/knowledge), comparison (a differentiator most agent frameworks lack), the reddit/standout feature list, demo script (a strong 10-sec beat: mount → agent edits → Apply-back diff).

**2. Toolset connectivity guard (a smaller, ops-posture beat).**
- Creating an MCP toolset now **probes the endpoint first** and **blocks unreachable ones** (no silently-broken toolsets in your catalogue), with a **"Create anyway"** escape hatch.
- **The angle:** production posture / operator ergonomics — the console catches misconfiguration at create time. A one-liner for the platform-team ICP; a footnote elsewhere. Don't over-feature it.

**3. Also shipped in 0.3.0 (mention lightly where relevant):** MCP-HTTP toolsets degrade gracefully instead of crashing the tools catalogue; `/v1/health` reports the real released version.

## Voice rules (non-negotiable)
- Honest, specific, concrete. NO hype, NO superlatives-without-substance. The thesis is a bet — say "thesis / I'd like people to try it and tell me where it breaks."
- Keep the drafts' existing structure, headings, and any per-sub / per-channel tailoring. You are updating, not replacing.
- Don't paste identical text across subreddits (that's already a rule in reddit.md — preserve it).
- Real feature names, real commands. Concede limitations (it's early — 0.3.0).

## Version / copy rules (apply everywhere)
- Every "**0.2.0**" → "**0.3.0**"; "0.2.0 is live" → "0.3.0 is live"; any "v0.2" → "v0.3".
- Install line stays `pipx install 'primer-ai[full]'` (or `pip install primer-ai`); pin examples to `==0.3.0` only where a pinned version is shown.
- Repo: `https://github.com/primerhq/primer`. GitHub releases now exist for v0.3.0 (Latest) + v0.2.0 — you may link the v0.3.0 release where a changelog link fits.
- Leave the honest "early (v0.3)" framing intact.

## What NOT to touch
- Don't invent metrics, benchmarks, or testimonials.
- Don't remove the drafts' honesty caveats or the risk/do-not sections.
- `journey-source.md` is raw material for the user's first-person article — leave it (it's evergreen origin-story material), unless a version reference is stale.
