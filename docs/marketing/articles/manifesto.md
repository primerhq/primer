# What is graph engineering?

Prompt engineering is the craft of one turn. You have a task, a model, and a text box, and you work the wording until the output holds. It is real skill, and for a single question it is often enough.

But most of the work worth doing is not one turn. It is a system that runs for hours or days: it wakes up, looks at the world, does several things at once, checks whether they worked, and either continues or asks a person.

It is not one loop either. That is the part most orchestration gets wrong. Real work branches on what came back. It fans out across a dozen items and rejoins. It calls a smaller, self-contained procedure in the middle. It stops for a human at exactly one step and runs the rest unattended. Draw that honestly on a whiteboard and you have not drawn a circle. You have drawn a graph.

Call that graph engineering: designing the structure the agents run inside, instead of hand-writing each prompt. The work shifts from the wording of one turn to the shape of the whole run. Which agent handles which step. What runs at the same time. Where it branches. Who signs off. And, yes, where it circles back, because a loop is still there. It is a cycle in the graph. It just stopped being the whole picture.

## The budget you are actually spending

Start with the constraint that shapes everything else.

A language model spreads a fixed budget of attention across every token in its context at once. Every token competes with every other token for that budget. Keep the context tight and the few tokens that matter get most of the attention. Bloat it with stale history, unused tool definitions, and irrelevant background, and the signal thins out. The model is still reading all of it, just paying less to each piece.

This is the core bet behind everything that follows: you often do not need the biggest model if you give the model you have exactly what it needs and nothing more. Context is a lever on any model, large or small.

It also explains why the graph is the right unit, and not the loop. A naive loop grows its own context. Each iteration appends to a transcript that never resets, and far enough in the model spends most of its budget re-reading its own history. The usual patch is to summarise the transcript, which is a lossy fix for a structural problem.

A graph does not have that problem, because the boundary between nodes is a boundary between contexts. Each node is its own agent with its own clean, purpose-built context: its own instructions, its own tools, its own model. What crosses the edge between them is a value you chose, not a transcript that accumulated. Splitting a long job into a researcher, an extractor, and a judge is not just tidier to read. It is three small contexts instead of one enormous one, and each of them can run on a model sized for its actual job.

## What a durable graph is made of

If the prompt is no longer the unit of work, what is? A graph that survives contact with production needs a specific set of parts. None of them is exotic. The trick is that you need all of them, and they have to fit together.

**A topology.** The structure itself has to be a thing you declare, not control flow buried in a script. Nodes and edges, written down, versioned, and readable by someone who did not write them. The moment the shape lives in code, the questions that matter get hard to answer: what runs in parallel, what happens when step four fails, where does a person get a say.

**A heartbeat.** Something has to start a run, on a schedule, after a delay, or when a webhook fires, instead of a person pressing "run". Work should surface itself.

**Isolation.** Parallel branches that share a filesystem will collide, and a graph that fans out is parallel by construction. Each agent needs its own sandbox, local, a container, or a Kubernetes pod, with its own persistent, git-backed state. Isolation is also blast radius: one branch that corrupts its workspace should not take the others down.

**Durable memory.** The model forgets everything between runs; the graph cannot. Memory has to live outside the context window, in git-backed workspace state and in retrievable collections the agent reads on demand. Done right, knowledge compounds across runs instead of resetting to zero, and it does that without inflating the next prompt: the agent pulls the relevant chunk when it needs it rather than carrying the whole corpus around.

**A maker and a checker.** An agent grading its own work is a weak control. Separate the roles: one node produces, another checks the output against evidence, and the edge between them only advances when the check passes. This is the producer-judge pattern, and it is the smallest interesting graph there is: two nodes and a conditional edge that either delivers, retries, or escalates.

**Connectors.** A graph that cannot reach real tools and real people is a demo. It needs to call tools, and it needs to reach a human where the human already is, a chat message rather than a dashboard nobody watches.

**A human gate.** Full autonomy is the wrong default for anything that spends money, ships code, or emails a customer. The graph should run the safe nodes on its own and stop at the risky one, and it should be able to wait there for hours without burning compute. Approve the risky, let the safe run.

Notice what these have in common. Each keeps every node's working context clean while the *system* around it holds the state: the topology is structure, the heartbeat is time, isolation is space, memory is history, the checker is judgment, the gate is authority. The intelligence is in the graph, not only in the prompt.

## Why now

Two things changed at once.

Small models got good enough to do real work when they are pointed precisely. A focused agent with a clean context and three relevant tools now clears a bar that used to want a frontier model and a page of instructions. That makes fleets of small agents worth reaching for, if you can orchestrate them. And it changes what a node costs: when a step is cheap and narrow, splitting one big agent into six specialised ones stops being an indulgence.

The orchestration is the gap. The industry spent two years on better single turns: prompt libraries, longer context windows, better function calling. Those help the turn. They do little for the structure around it. The parts a durable graph needs are still mostly something each team rebuilds by hand on top of a framework designed for one agent in one process, which is why so much agent orchestration ends up as a while loop with if-statements in it. That shape is not chosen. It is what you get when the framework has no way to express anything else.

Longer context windows, in particular, are not the fix here. A bigger window lets you make the attention-budget problem worse before you feel it. The discipline still has to come from the structure.

## The substrate

We built Primer because we wanted these primitives to exist as infrastructure instead of as glue code.

Primer is a self-hosted control plane for fleets of small, context-optimized agents, and its execution model is a graph. You declare a topology of typed nodes: agents, sub-graphs, fan-out and fan-in for map-reduce, direct tool calls, and pure data-shaping entry and exit nodes with their own schemas. Edges are static or conditional, routed on the actual content of what a node returned. A Pregel-style executor walks it in supersteps, running every ready node concurrently, and cycles bound themselves with an iteration cap. Each node pins its own model, its own tools, and its own output contract, which is what keeps the contexts separate rather than merely adjacent.

The rest of the parts are first-class things you run, not patterns you reimplement: **triggers** for the heartbeat; local, container, and **Kubernetes workspaces** for isolation; git-backed workspace **state** and **semantic-search collections** for durable memory; a built-in **MCP server** (and client) plus **Slack / Telegram / Discord** channels for connectors; **approval gates** and mid-graph **park-and-resume** for the human gate, so a run can wait days at one node without holding a process open. It is Apache-2.0, early, and it runs on your own hardware.

Primer does not decide the shape for you. That is deliberate. It is the substrate you draw the graph on, and the place you keep a human in it where that matters. The judgment about what to split, what to run in parallel, what to check, and where to stop is the engineering. The platform's job is to make that judgment expressible, and to keep every node's context clean so the run can go a long time without drifting.

That is the shift. Prompt engineering got us a good turn. Graph engineering is how you get a system that keeps taking good turns, in the right order, for as long as the work lasts.

<!-- editor notes: (1) "Small models got good enough to do real work" is an industry-trend assertion, not a Primer benchmark, kept deliberately unquantified; confirm you're comfortable stating it as opinion. (2) "three relevant tools" and "a page of instructions" are illustrative figures, not measurements. (3) Everything Primer-specific maps to a shipped capability: the seven node kinds, Pregel supersteps with concurrent ready nodes, static/conditional edges with content routing, max_iterations on cycles, per-node model profile + response_format, and mid-graph park/resume are all in the graphs subsystem doc. No invented features. (4) Primer appears only in the final two sections as a soft tie-in, per brief. (5) Version number deliberately dropped from the substrate paragraph so the piece does not go stale on each release. -->
