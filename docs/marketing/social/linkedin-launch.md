# LinkedIn launch post

> Professional, narrative, platform-team framing. Post from your personal profile (higher reach than a company page for a launch). **Attach the Studio console screenshot** `docs/marketing/assets/studio-console-hero-dark.png` (dark-mode graph run + live event rail). Put the repo link in the FIRST comment if you want to maximize reach — LinkedIn suppresses posts with outbound links in the body; alternatively keep it in-body for clarity. Your call.

## Post

For the last few months I've been building **Primer** — and today it's open source (Apache-2.0).

It started with a constraint. I wanted to run useful AI agents on a 16GB gaming GPU, not a rented frontier API. That hardware caps you around a 12B model — which doesn't win a head-on fight. So Primer is built on a different bet:

**A small model given a clean, purpose-built context can rival a much larger one.**

Every transformer spends a fixed budget of attention across its tokens. A tight, deliberate context puts more of that budget on what actually matters (and yes — that helps big models too). I call it *loop engineering*: the value isn't the model, it's the loop you build around it.

Primer is the **self-hosted control plane** that makes that practical at fleet scale. Not a library you import — a platform you run:

→ **Park-and-resume** agents that wait hours on a tool or a human decision without holding compute
→ **Directed graphs** — producer/judge loops, fan-out/fan-in — as first-class structure, not glue code
→ **Workspaces** (local / container / **Kubernetes**) with a git-backed filesystem, so agent state lives *outside* the context window
→ **MCP-native** — a built-in MCP server *and* client, so you can drive the platform with agents
→ **Collections that mount as editable files** — knowledge your agents read, write, and sync back, versioned like code
→ **Channels + human approvals** for the steps that genuinely need a person
→ An **operator console** to launch, observe, and debug all of it

It's early (v0.3), self-hosted, and honest about being a thesis I'm testing — not a benchmarked result. If you're building agent systems, or thinking about production posture, isolation, and ops for them, I'd love your eyes on it.

⭐ Repo + quickstart: https://github.com/primerhq/primer

What would you want to see a small-agent fleet do first?

#AI #LLM #AIAgents #OpenSource #MCP #SelfHosted #MachineLearning #DevTools

---
**Notes:**
- The closing question ("What would you want to see…") is there to prompt comments — LinkedIn's algorithm heavily rewards early comment velocity. Reply to every comment in the first 2 hours.
- If you split the link to the first comment, replace the in-body link line with: "Repo in the comments 👇".
- Keep it to one screenshot; carousels underperform for a launch announcement.
