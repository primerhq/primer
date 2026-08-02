# GitHub Discussions — seed content (item 5)

> Enable Discussions first: repo **Settings → Features → check "Discussions"**. Then set up categories and seed each with your own first post, so an arriving visitor sees an active, cared-for space (empty Discussions reads as abandoned). Pin the Announcement. Post these BEFORE the Reddit/LinkedIn wave drives traffic.

## Categories to enable (Settings → Discussions → Categories)
Keep the defaults and make sure these exist:
- **📣 Announcements** (maintainer-only post; anyone can comment) — releases, launch
- **💬 General** — open chat
- **🙏 Q&A** (question/answer format) — support, "how do I…"
- **💡 Ideas** — feature requests, roadmap input
- **🙌 Show and tell** — what people build with Primer
- **🗺️ Roadmap** (optional; or use a pinned Ideas post)

---

## 1. 📣 Announcements — PIN THIS
**Title:** Primer 0.3.0 is here — and the project is now open (start here 👋)

**Body:**

Welcome, and thanks for stopping by. Primer is a **self-hosted control plane for running fleets of small, context-optimized AI agents** — MCP-native, with an operator console, park-and-resume execution, graphs, workspaces, and human-in-the-loop approvals.

**The bet behind it:** a small model given a clean, purpose-built context can rival a much larger one. That's a thesis I'm testing in the open, not a benchmarked claim — and I'd genuinely love for you to try it and tell me where it breaks.

**Get started**
- Install: `pipx install 'primer-ai[full]'` (or `pip install primer-ai`)
- Quickstart: https://github.com/primerhq/primer#quickstart
- Console: run it, then open `/console`

**Where to go from here**
- Questions / stuck on setup → **Q&A**
- A feature you wish existed → **Ideas**
- Built something → **Show and tell** (I will boost it)
- Found a bug → open an **Issue**

It's early (v0.3). The golden path is solid; it gets rougher the further out you go. That's exactly the feedback I want. — [your name]

---

## 2. 💡 Ideas — roadmap starter (pin in the Ideas category)
**Title:** Roadmap & what's next — tell me what to prioritize

**Body:**

Here's roughly where my head is for the next few releases. **React with 👍 on the items you'd use**, and drop new ideas as separate posts in this category.

Candidate directions (unordered, not committed):
- Richer graph authoring in the console (branch/merge nodes, retries, sub-graphs)
- More first-class channels + approval policies
- Deeper collection/workspace round-tripping (per-file apply, conflict resolution UI)
- More harness examples + a small public gallery
- Model provider presets for common local setups (llama.cpp / Ollama / vLLM)
- Observability: run timelines, token/latency accounting per node

What's missing? What would make you actually deploy this? I read everything here.

---

## 3. 🙏 Q&A — seed the two questions everyone asks
**Post A — Title:** How is Primer different from LangGraph / CrewAI / AutoGen?

**Body:**
Short version: those are **libraries you import into your app**. Primer is a **self-hosted platform you run** — it owns the execution loop, isolation (workspaces on local/container/k8s), a built-in MCP server + client, channels, approvals, and an operator console. You get an ops surface, not just an SDK. If you want a Python function to call, use a library. If you want to *operate a fleet of agents* — run, observe, pause, approve, debug — that's what Primer is for. Ask follow-ups below.

**Post B — Title:** What hardware / models does Primer target?

**Body:**
It was born on a 16GB gaming GPU (RTX 5060 Ti), which caps you around a 12B model at 4-bit — so the whole design leans on clean, tight contexts rather than raw model size. It's model-agnostic: point it at a local runtime or a hosted API. The interesting question isn't "biggest model" — it's "how good a loop can you build around a small one." Share your setup and what worked.

---

## 4. 🙌 Show and tell — prime the pump
**Title:** Show us your loops — what have you built with Primer?

**Body:**
Mount a knowledge base and let an agent maintain it? A producer→judge graph that drafts and self-critiques? A Slack-approved deploy agent? Post it here — a screenshot, a harness bundle, a short clip. I'll feature the good ones in the README and release notes. Bragging encouraged.

---

## Sequencing
1. Enable Discussions + categories.
2. Post #1 (Announcements) and **pin** it.
3. Post #2 (Ideas/roadmap), #3 A+B (Q&A), #4 (Show-and-tell).
4. THEN launch on Reddit/LinkedIn/HN — arrivals land on a living space.
5. Answer every real question within a few hours on launch day.
