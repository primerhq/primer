# Reddit launch post — the journey + new-approach version

> First-person story post. **Adapt the opening per sub, and never paste identical text into two subs** (fastest way to get flagged). Great fit for r/LocalLLaMA, r/LLMDevs, r/AI_Agents, r/selfhosted, r/Python.
> **Screenshot to attach:** `docs/marketing/assets/studio-council-hero-dark.png` — the Studio console showing a live agent-graph run with the event rail.

## Title (your pick)
**Here's a new approach to AI agent platforming that lets you build and run deterministic agent loops**

Alternates if you want to A/B:
- I wanted useful agents on a 16GB GPU, so I built a platform for running deterministic loops of small agents
- A different take on agent platforms: directed cyclic graphs, yielding tools, and a searchable tool bus

---

## Body

It all started with me wanting to get some genuinely useful work out of the small open-weight models I can actually run on my own hardware. I've got a 16GB GPU, which caps you at around a 12B model. I tried the existing agent frameworks and platforms and honestly none of them did it for me, so I ended up building my own. It's called **Primer**.

The core bet I'm making with this thing: **a small model, given a clean, purpose-built, optimized context, can actually get meaningful work done.** Not "replace a frontier model" — just genuinely useful work. It's a bet, not a benchmark, and I'd love for people to try it and tell me where it falls apart.

Once I had that principle, the whole thing became about one question: how do you get real work out of small agents *despite* their limits? Chasing that pushed me into a pretty different way of building agents, which is what the platform ended up being. Here's the gist.

### The novel stuff (the parts I haven't really seen elsewhere)

- **Directed cyclic agent graphs.** This is the core idea. I figured that if you run a bunch of small agents in a feedback loop, you're basically trading compute for time — the loop keeps running until it reaches the state you want. So I built graphs of agents that are directed *and* cyclic. The move that makes it click: you can put an **evaluator agent at the end of the loop** that judges the quality of the output and feeds it back to the first agent. Producer → critique → revise, until it's actually good. That's the "loop engineering" bit, and it's how you get a loop that *converges on a target* instead of a one-shot prompt you just hope lands.

- **Shared workspaces.** I wanted multiple agents and graphs to work independently but still share what they find. Simplest thing I could come up with: let them share the same sandbox. So you can run multiple agents and graphs in one workspace, all reading and writing the same filesystem.

- **Yielding tools.** Loops and graphs are meant to run in the background — I don't want to sit in front of a screen keeping a session open. So an agent needs to be able to *stop* when it needs input or has to wait on something. I built a class of tools that **yield control out of the agent and park it until an event fires**. That's what makes long-running, event-driven agents work. Nice bonus: agents can watch files in the shared workspace, so one agent writes to a file and another wakes up on the change.

- **An internal search subsystem for tools.** If you want context-optimized agents, you can't just register tens of tools on each one — the tool definitions themselves bloat the context. So instead I register **all** tool definitions as vector embeddings and give the agent two meta-tools: one to **search** for the capability it needs, and a `call_tool` to actually **invoke** any tool in the platform. Two tools in context, access to all of them. Then I generalized it, so an agent can search for and call any other **agent, graph, or tool** in the system.

- **Dogfooding is first-class.** The platform's own capabilities are exposed as internal tools too, so I can build agents that build other agents and graphs *on* the platform.

- **An MCP endpoint over everything.** Honestly, the UI is starting to feel like the old way of doing things — the new experience is just asking an agent to do what you want and expecting it to get there. I hit that myself; I wanted to drive Primer from Claude and opencode. So I exposed the whole platform over **MCP**. You operate it *with* agents, not just point it at tools.

### The standard stuff it also ships

- Vector collections (knowledge bases)
- MCP-server toolsets
- Tool-approval controls (human in the loop on sensitive calls)
- First-class web search
- Channels: Slack, Telegram, Discord
- Triggers (cron / delay / webhook)
- Harnesses (package + share a whole agent setup)

It's still very much a **work in progress** — early, rough in places, and the small-model bet is still a bet. I'd honestly, humbly love for people to give it a shot, share feedback, and file any bugs you run into (especially where the whole small-model idea breaks down for your workload).

Repo's here: **https://github.com/primerhq/primer** — Apache-2.0, self-hosted, `pipx install 'primer-ai[full]'`.

Thanks for reading. Happy to answer anything in the comments.

---
**Rules of the road:** be present to reply for a few hours after posting; concede limitations freely (it builds trust); never ask for upvotes; if someone asks "isn't this just LangGraph?", the honest distinction is *a self-hosted platform you run with an ops surface + a tool/agent bus + background execution, not a library you import.*
