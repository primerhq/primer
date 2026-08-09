# Awesome-list catalog

Where primer can be submitted, where it will be rejected today, and in what
order. Researched 2026-08-04 against live GitHub data.

Companion to `article-distribution-research.md`, which covers publishing and
distribution. This covers directory listings only.

---

## Read this before opening any PR

**Primer's numbers as of 2026-08-04:** 14 stars, 1 fork, created 2026-06-23
(6 weeks old), Apache-2.0, actively developed (0.6.0 shipped today).

That combination decides everything below. The large lists gate on
popularity, and a 14-star submission to `vinta/awesome-python` is not a
close call - it is a rejection, and it costs goodwill with a maintainer you
may want later. The lists worth your time now are the ones that gate on
**fit** rather than stars.

Set expectations accordingly: a merged awesome-list entry is a slow trickle,
not a spike. The HN/Reddit plan in `article-distribution-research.md` is a
much bigger lever. Treat these as compounding background placement, and do
them when you have 20 spare minutes, not as launch-week work.

**The one rule that matters:** submit where primer genuinely belongs. Awesome
lists are maintained by people who read every PR, and a project filed under a
category it does not fit is remembered. One good entry beats six rejections.

---

## Status (updated 2026-08-04, after reading each list's actual rules)

Researching the submission templates disproved three assumptions in the first
draft of this catalog. Recorded here rather than quietly edited, because the
reasons generalise.

| List | Verdict | Why |
|---|---|---|
| `punkpeye/awesome-mcp-servers` | **SUBMITTED** - [PR #11466](https://github.com/punkpeye/awesome-mcp-servers/pull/11466) | Added under Aggregators |
| `e2b-dev/awesome-ai-agents` | **SUBMITTED** - [PR #1349](https://github.com/e2b-dev/awesome-ai-agents/pull/1349) | Open-source section |
| `awesome-selfhosted-data` | **BLOCKED until ~2026-10-23** | Requires first release >4 months ago; primer is 42 days old |
| `wong2/awesome-mcp-servers` | **Not a PR** | "We do not accept PRs" - submit at https://mcpservers.org/submit |
| `vinta/awesome-python` | Wait (~150 stars) | Hidden Gem tier prefers 100-500 stars |

### The 4-month rule is the important one

`awesome-selfhosted` requires that a project "was first released more than 4
months ago". Primer's first release was 2026-06-23, so it is **42 days old**
and ineligible until roughly **2026-10-23**. The first draft of this catalog
ranked it as the #1 target with "odds: good" - that was wrong, and a PR would
have been closed.

Worth checking the same field on any list added here later: maturity gates are
common in the older, better-curated lists and are invisible until you open the
submission template.

### Submitted: punkpeye/awesome-mcp-servers

Category **Aggregators** ("servers for accessing many apps and tools through a
single MCP server"), which is an honest fit: primer re-exposes built-in
toolsets, registered MCP servers and python tools through one MCP server,
gated by a per-tool allowlist. Verified against `primer/mcp/dispatch.py`
before claiming it.

Markers: `🎖️ 🐍 🏠 🍎 🐧`. No `☁️` (no hosted offering), no `🪟` (sandbox
backends target Linux and macOS). Inserted alphabetically between
`portel-dev/ncp` and `profullstack/mcp-server`.

Used the CONTRIBUTING's agent fast-track (`🤖🤖🤖` in the title), which that
maintainer explicitly asks agent-authored PRs to opt into.

### Submitted: e2b-dev/awesome-ai-agents

Full entry block (heading, tagline, `<details>` with Category / Description /
Links), alphabetical between **Pezzo** and **Private GPT**. Category:
`General purpose, Multi-agent, Build-your-own`.

The pitch leans on isolation and the honest isolation-level reporting, since
e2b is a sandboxing company and that is the part of primer their readers care
about.

### Waiting: awesome-selfhosted-data

Entry is a YAML file at `software/primer.yml`, not a markdown line. Template
in `.github/ISSUE_TEMPLATE/addition.md`. Draft it near the eligibility date:

```yaml
name: "Primer"
website_url: "https://primerhq.github.io/"
source_code_url: "https://github.com/primerhq/primer"
description: "Self-hosted control plane for fleets of small, context-optimized agents: graphs, workspaces, channels, triggers, MCP."
licenses:
  - Apache-2.0
platforms:
  - Python
  - Docker
  - Kubernetes
tags:
  - Automation
  - Software Development
```

Check `platforms/` and `tags/` for the exact spellings before submitting; a
tag that does not already exist needs 3 projects referencing it.

### Not a PR: wong2/awesome-mcp-servers

Its README states plainly: **"We do not accept PRs. Please submit your MCP on
the website: https://mcpservers.org/submit"**. Needs a human with a browser.

## Tier 2 - worth doing, smaller return

Real lists, active, but narrower reach. Batch these in one sitting.

| List | Stars | Idle | Fit |
|---|---|---|---|
| `Jenqyang/Awesome-AI-Agents` | 1.2k | 9d | Agent frameworks and platforms |
| `kaushikb11/awesome-llm-agents` | 1.6k | 1d | Very active; agent-focused |
| `TensorBlock/awesome-mcp-servers` | 801 | 0d | Third MCP list, updated daily |
| `InftyAI/Awesome-LLMOps` | 255 | 1d | LLMOps framing fits "control plane" |
| `bh-rat/awesome-mcp-enterprise` | 117 | 5d | Enterprise MCP; self-hosting is the pitch |
| `caramaschiHG/awesome-ai-agents-2026` | 1.5k | 54d | Check it is still curated before submitting |

---

## Tier 3 - wait

Primer does not meet the published bar today. Revisit at the star counts
below rather than submitting and being turned down.

### vinta/awesome-python (312k stars)
Published acceptance criteria, one of which must hold:
1. **Industry Standard** - the go-to tool almost everyone uses
2. **Rising Star** - 5,000+ stars in under a year
3. **Hidden Gem** - "100-500 stars preferred; **< 100 requires strong
   justification**"

At 14 stars primer fails all three. Also requires the repo to be at least
1 month old (primer passes that one). **Revisit at ~150 stars** and submit
under Hidden Gem with a real argument about the category gap.

### Shubhamsaboo/awesome-llm-apps (130k stars)
Not a link directory - it is a collection of runnable app implementations
with source in-repo. Submitting a platform link is the wrong shape. If you
want in, the path is contributing an **example app built on primer**, which
is a content project, not a listing.

---

## Skip

Stale, and a PR into a dead list is worse than no PR - it sits open and
signals nothing.

| List | Stars | Idle |
|---|---|---|
| `tensorchord/Awesome-LLMOps` | 5.9k | 74d |
| `appcypher/awesome-mcp-servers` | 5.7k | 89d |
| `slavakurilyak/awesome-ai-agents` | 2.1k | 328d |
| `jim-schwoebel/awesome_ai_agents` | 1.9k | 128d |
| `rohitg00/awesome-devops-mcp-servers` | 1k | 83d |
| `chatmcp/mcpso` | 2.1k | 495d |
| `PipedreamHQ/awesome-mcp-servers` | 282 | 491d |

Also skip the `-cn`, `-aws` and personal-fork variants of awesome-selfhosted:
they are mirrors or abandoned.

---

## Suggested order

1. **awesome-selfhosted-data** - highest value, YAML entry, take the time
2. **punkpeye/awesome-mcp-servers** - largest MCP audience
3. **wong2/awesome-mcp-servers** - same content, second MCP list
4. **e2b-dev/awesome-ai-agents** - best-matched audience
5. Tier 2 as one batch, later

Do not open all of these on the same day. Several maintainers watch each
other's lists, and a simultaneous six-list push reads as promotion rather
than contribution - the same dynamic as the 9:1 rule in
`article-distribution-research.md`.

## What each entry should say

Reuse the repo description, which `positioning.md` already settled:

> Self-hosted control plane for fleets of small, context-optimized agents:
> graphs, workspaces, channels, triggers, MCP.

For MCP lists, lead with the MCP server rather than the control plane - that
is what those readers are scanning for. For awesome-selfhosted, lead with
self-hosted and the operator console.

## Open question

Whether to submit at all before the launch push. An entry added at 14 stars
is judged on fit alone; the same entry added at 300 stars is judged on
momentum too, and momentum is what gets a maintainer to say yes quickly.
Nothing here expires, so there is an argument for doing awesome-selfhosted
now (it genuinely does not care about stars) and holding the rest until
after the HN launch.
