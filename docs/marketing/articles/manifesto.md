# What is loop engineering?

Prompt engineering is the craft of one turn. You have a task, a model, and a text box, and you work the wording until the output holds. It is real skill, and for a single question it is often enough.

But most of the work worth doing is not one turn. It is a system that runs for hours or days: it wakes up, looks at the world, does something, checks whether the something worked, and either continues or asks a person. The prompt is now the smallest part. The hard part is the loop around it.

Call that loop engineering: designing the system that prompts the agent, instead of hand-writing each prompt. The work shifts from the wording of one turn to the structure of the whole run — when it wakes, what it can see, where its output goes, who checks it, and when it stops to ask.

## The budget you are actually spending

Start with the constraint that shapes everything else.

A language model spreads a fixed budget of attention across every token in its context at once. Every token competes with every other token for that budget. Keep the context tight and the few tokens that matter get most of the attention. Bloat it with stale history, unused tool definitions, and irrelevant background, and the signal thins out — the model is still reading all of it, just paying less to each piece.

This is the core bet behind everything that follows: you often do not need the biggest model if you give the model you have exactly what it needs and nothing more. Context is a lever on any model, large or small.

It also sets a hard rule for long-running work. A naive loop grows its own context — each iteration appends to a transcript that never resets. Far enough in, the model spends most of its budget re-reading its own history, and accuracy drifts. A loop meant to run for a long time has to be built so that each iteration gets a clean, purpose-built context, not an ever-growing log. That single requirement drives most of the design.

## What a durable loop is made of

If the prompt is no longer the unit of work, what is? A loop that survives contact with production needs a specific set of parts. None of them is exotic. The trick is that you need all of them, and they have to fit together.

**A heartbeat.** Something has to wake the agent — on a schedule, after a delay, or when a webhook fires — instead of a person pressing "run." Work should surface itself. Without a heartbeat you do not have a loop; you have a script you keep re-running by hand.

**Isolation.** Parallel agents that share a filesystem will collide. Each one needs its own sandbox — local, a container, or a Kubernetes pod — with its own persistent, git-backed state. Isolation is also blast radius: one agent that corrupts its workspace should not take the others down.

**Durable memory.** The model forgets everything between runs; the loop cannot. Memory has to live outside the context window — in git-backed workspace state and in retrievable collections the agent reads from on demand. Done right, knowledge compounds across runs instead of resetting to zero. And it does that without inflating the next prompt: the agent pulls the relevant chunk when it needs it, rather than carrying the whole corpus around. Increasingly that outside state is not just read from — a collection can be mounted into a workspace as live files an agent edits directly, so what it learns gets written back, not only retrieved and forgotten.

**A maker and a checker.** An agent grading its own work is a weak control. Separate the roles: one part produces, another checks the output against evidence, and the loop only advances when the check passes. This is the producer-judge pattern, and it is the difference between "the model said it was done" and "the output cleared a bar."

**Connectors.** A loop that cannot reach real tools and real people is a demo. It needs to call tools, and it needs to reach a human where the human already is — a chat message, not a dashboard nobody watches.

**A human gate.** Full autonomy is the wrong default for anything that spends money, ships code, or emails a customer. The loop should run the safe steps on its own and stop for a person on the risky ones — and it should be able to wait for that person for hours without burning compute while it waits. Approve the risky, let the safe run.

Notice what these have in common. Each keeps the model's working context clean while the *system* around it holds the state: the heartbeat is time, isolation is space, memory is history, the checker is judgment, the gate is authority. The intelligence is in the loop, not only in the prompt.

## Why now

Two things changed at once.

Small models got good enough to do real work when they are pointed precisely. A focused agent with a clean context and three relevant tools now clears a bar that used to want a frontier model and a page of instructions. That makes fleets of small agents worth reaching for — if you can orchestrate them.

The orchestration is the gap. The industry spent two years on better single turns: prompt libraries, longer context windows, better function calling. Those help the turn. They do little for the loop. The parts a durable loop needs — the heartbeat, the isolation, the durable memory, the checker, the gate — are still mostly something each team rebuilds by hand on top of a framework designed for one agent in one process.

Longer context windows, in particular, are not the fix here. A bigger window lets you make the attention-budget problem worse before you feel it. The discipline still has to come from the system.

## The substrate

We built Primer because we wanted these primitives to exist as infrastructure instead of as glue code.

Primer is a self-hosted control plane for fleets of small, context-optimized agents. It gives you the parts above as first-class things you run, not patterns you reimplement: **triggers** for the heartbeat; local, container, and **Kubernetes workspaces** for isolation; git-backed workspace **state** and **semantic-search collections** for durable memory; **directed cyclic graphs** for producer-judge loops; a built-in **MCP server** (and client) plus **Slack / Telegram / Discord** channels for connectors; **approval gates** and **park-and-resume** for the human gate. It is Apache-2.0, early — v0.3 — and it runs on your own hardware.

Primer does not press "go" on the loop for you. That is deliberate. It is the substrate you build the loop on, and the place you keep a human in it where that matters. The judgment about when to wake, what to check, and when to stop is the engineering. The platform's job is to make that judgment expressible — and to keep every iteration's context clean so the loop can run a long time without drifting.

That is the shift. Prompt engineering got us a good turn. Loop engineering is how you get a system that keeps taking good turns, on its own, for as long as the work lasts.

<!-- editor notes: (1) "Small models got good enough to do real work" is an industry-trend assertion, not a Primer benchmark — kept deliberately unquantified; confirm you're comfortable stating it as opinion. (2) "three relevant tools" and "a page of instructions" are illustrative figures, not measurements. (3) Everything Primer-specific maps to a README/positioning capability; no invented features. (4) Primer appears only in the final two sections as a soft tie-in, per brief — if you want it even softer, the "The substrate" heading can be renamed or trimmed. -->
