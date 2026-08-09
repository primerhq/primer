# "My journey building Primer" — article source & outline

> Raw material for YOUR first-person piece (Medium/LinkedIn/own blog). It's built from
> `docs/dev/vision/` — which is already written as a first-hand narrative, so most of the
> work is making it personal and cutting it to length. Keep it honest (the thesis is a bet,
> not a settled result — that candor is what makes it credible).

## The hook (this is your opening — it's a great one)
You wanted to run open-weight models **locally, on a gaming PC you already owned** — an
RTX 5060 Ti with **16 GB of VRAM**. That ceiling is hard: it caps you at roughly a **12B
model quantized to 4-bit**. And a quantized 12B model plainly does *not* compete with a
frontier model on reasoning, long-horizon tasks, or tool use.

The naive conclusion: give up and rent a frontier API. The interesting conclusion — the
one Primer is built on — is different. **That tension is your first paragraph.**

## The turn (the bet)
> A small model given the *perfect context* for a single, narrow task can approach the task
> accuracy of a frontier model on that same task.

Why it's even plausible: a transformer has **one unit of attention to spend per token**,
spread across the whole context (softmax → sums to one). In a tight 200-token context, the
5 tokens that matter get a big clean signal. In a bloated 50,000-token context, those same
5 compete with 49,995 distractors — the signal thins. ("Lost in the middle," context rot.)
The kicker: **every transformer suffers this, frontier models included** — so cleaning up
context isn't a crutch for weak hardware, it's a lever on the whole class of models.

## The build (each subsystem = removing one obstacle)
This is the spine of the piece — you built Primer *as a narrative*, each piece answering a
problem the last one exposed. Walk 4–6 of these (don't do all 9 — pick the vivid ones):
1. **Microagents** — decompose the task into many small, single-purpose agents instead of one big one.
2. **Tool routing** — a short tool list per agent + two meta-tools so one agent can reach every tool without carrying the whole catalog in its context.
3. **Workspaces** — a shared file/process sandbox (local/container/k8s) so state lives *outside* the context window.
4. **Graphs** — directed, cyclic graphs; the **producer/judge loop** the thesis most wanted to test.
5. **Event-driven execution** — trade compute for time: yielding tools, **park/resume**, `ask_user`, scheduled triggers.
6. **Harnesses** — package a tuned config into a portable bundle ("Helm for Primer").

## The honest close (keep this — it's your credibility)
> The claim that orchestrated small models can match a frontier model on a decomposed task
> is a hypothesis, not a settled result. Primer is the apparatus built to test it. If it
> holds, the techniques help frontier models too. If it only half-holds, it's still a clean
> way to build multi-agent systems on modest hardware.

## Suggested structure (900–1400 words)
1. **The 16GB wall** (hook — the PC, the ceiling, the uncomfortable fact) — ~150w
2. **The bet** (context quality vs scale; the attention-budget intuition, plainly) — ~250w
3. **Building it as a chain** (4–6 subsystems, one paragraph each, what problem each solved) — ~500w
4. **What I'd tell someone starting today** (the loop-engineering takeaway; small contexts, decompose, sequence with structure, let time do the work) — ~200w
5. **Honest status + call to action** (it's a thesis; here's the repo; try it / tear it apart) — ~150w

## Pull-quotes you can use verbatim (all from your own vision docs)
- "16 GB of VRAM is a hard ceiling. It sets, very precisely, how large a model you can hold in memory and run at usable speed."
- "The model has exactly one unit of attention to spend per token, and it must spread that single unit across the entire context."
- "Optimizing context is not a crutch for weak hardware. It is a lever on the whole class of models."
- "Primer was built as a narrative: each subsystem is the answer to a problem the previous one exposed."

## Titles to test
- *I tried to run AI agents on a 16GB gaming GPU. It changed how I build with LLMs.*
- *The bet behind Primer: a small model with a clean context can rival a much bigger one*
- *Graph engineering: what building an agent platform on modest hardware taught me*

## Don't
- Don't claim benchmarked parity — it's a thesis; say so (that line IS the trust).
- Don't bury the gaming-PC hook — it's the most human, most shareable part.
- Don't turn it into a feature list — it's a *story*; the features are beats in it.
