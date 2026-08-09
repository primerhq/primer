# High-impact subreddits + the standout-feature post

> Companion to `reddit.md` (which has per-sub tailored drafts). This adds the *targeting*
> (which subs, in what order, why) and one strong general post you can adapt.
> Reddit rules change and vary by sub — **re-check each sub's rules/wiki right before posting**,
> and never paste the same text into two subs (fastest way to get flagged).

## Targets, ranked by impact (reach × fit)

**Tier 1 — do these first (best fit for a self-hostable, local-first agent platform):**
| Sub | Why it fits | Angle |
|---|---|---|
| **r/LocalLLaMA** | large + exactly your people (self-host, small/open models). The 16GB-GPU origin story *lands* here. | local-first, own-your-stack, small-model bet |
| **r/AI_Agents** | the precise niche — people building agent systems, growing fast | orchestration, graph engineering, park/resume |
| **r/LLMDevs** | LLM engineers building real things | graphs, maker/checker, MCP |

**Tier 2 — strong fit:**
| Sub | Why | Angle |
|---|---|---|
| **r/mcp** | niche but a *perfect* fit; engaged MCP community, far less saturated than "agent framework" | MCP server + client, drive Primer from other agents |
| **r/selfhosted** | large; loves run-it-yourself infra | Docker/Postgres/k8s, Apache-2.0, console |
| **r/ChatGPTCoding** | dev builders shipping with LLMs | batteries-included, quickstart |
| **r/Python** | huge reach; FastAPI/architecture hook. Stricter self-promo — frame as "I built…" | the engineering story |

**Tier 3 — reach, but go last / most polished:**
| Sub | Note |
|---|---|
| **r/MachineLearning** | huge + skeptical; use a `[P]` project post, most rigorous framing, post **last** |
| **r/artificial**, **r/ArtificialIntelligence** | broad reach, lower signal |
| **r/opensource** | OSS-friendly; lead with Apache-2.0 + contribution |

**Be careful:** r/programming (huge but very strict on self-promo — easy to get flagged), r/singularity (low signal for a dev tool).

**Suggested order (stagger 2–4 days each):**
r/LocalLLaMA → r/AI_Agents → r/mcp → r/LLMDevs → r/selfhosted → r/ChatGPTCoding → r/Python → r/MachineLearning (last).

---

## The standout-feature post (adapt per sub; don't paste verbatim across subs)

**Title options:**
- I tried to run AI agents on a 16GB gaming GPU, so I built a platform for orchestrating *small* agents (open source)
- Primer: a self-hosted control plane for fleets of small, context-optimized agents
- Show: Primer — park-and-resume agents, directed graphs, MCP-native, self-hosted

**Body:**

> I wanted to run useful agents on hardware I already owned — a 16GB gaming GPU — which caps
> you at roughly a 12B model at 4-bit. A model that size doesn't compete with a frontier model
> head-on. So instead of one big agent with a giant prompt, I built around a bet: **a small
> model given a clean, purpose-built context can rival a much larger one** — because every
> transformer spreads a fixed budget of attention across every token, so a tight context puts
> more of that budget on the tokens that matter (and this helps big models too).
>
> Primer is the platform I built to work that way at fleet scale. What's actually in it:
>
> - **Park-and-resume agents** — an agent can wait on a slow tool or a human decision for
>   *hours* without holding compute, then resume when the event fires.
> - **Directed cyclic graphs** — producer→judge loops, fan-out/fan-in, conditional branches.
> - **Workspaces** — each agent gets a local / container / **Kubernetes** sandbox with a
>   persistent, git-backed filesystem, so state lives *outside* the context window.
> - **Collection ↔ Workspace mount** *(new in 0.3.0)* — mount a whole Collection (a
>   knowledge base of path-addressed documents) into an agent's workspace as a live,
>   editable directory. The agent reads and writes those files directly; a 3-way diff
>   preview + "Apply to collection" step syncs its edits back upstream when you're ready.
>   Knowledge as an agent-editable artifact, not a read-only vector blob.
> - **MCP-native** — a built-in MCP server *and* client, so you can drive Primer from other
>   agents, not just point it at tools.
> - **Channels + approvals** — Slack/Telegram/Discord; gate sensitive tool calls behind a
>   human who approves from a message or the console.
> - **Harnesses** — package a tuned set of agents/graphs/collections into a versioned bundle.
>
> Self-hosted, Apache-2.0, Python. `pipx install 'primer-ai[full]'` or a Docker image; there's
> an operator console at `/console`. It's **early (v0.3)** — the golden path is solid, rougher
> the further you get from it.
>
> To be upfront: "small model rivals a big one" is a **thesis I'm building around, not a
> benchmarked guarantee.** I'd genuinely like people to try it and tell me where it breaks.
>
> Repo: https://github.com/primerhq/primer

**Per-sub trims:**
- **r/LocalLLaMA / r/selfhosted:** keep the GPU hook + self-host; cut the MCP-for-agents line.
  If there's room, one line on the collection mount fits too — it's another own-your-stack
  angle (your docs live as files on your own disk, not in someone else's vector index).
- **r/AI_Agents / r/mcp:** lead with park-and-resume + MCP + the collection mount (mount your
  own docs into a workspace, let the agent edit them, diff-and-apply back); trim the hardware
  backstory to one line.
- **r/Python:** open with the FastAPI/architecture angle; the agent stuff is the "what it's for."
- **r/MachineLearning:** `[P]`, drop the install pitch, lean into the attention-dilution rationale + honest "thesis not result."

**Rules of the road:** be present to reply for hours; concede limitations (it builds trust); never ask for upvotes; answer "isn't this just LangGraph?" with the real distinction (a self-hosted *platform* with an ops surface, not a library) — don't get defensive.
