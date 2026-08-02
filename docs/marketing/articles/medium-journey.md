<!--
TITLE OPTIONS — pick one before publishing:
1. I tried to run AI agents on a 16GB gaming GPU. It changed how I build with LLMs.
2. The bet behind Primer: a small model with a clean context can rival a much bigger one
3. Loop engineering: what building an agent platform on modest hardware taught me
-->

# I tried to run AI agents on a 16GB gaming GPU. It changed how I build with LLMs.

### Why one graphics card's worth of VRAM turned into a bet about how attention works — and a platform built to test it.

I built Primer because of a graphics card.

Specifically: an RTX 5060 Ti with 16 GB of VRAM, sitting in a gaming PC I already owned. I wanted to run open-weight language models locally and get real work out of them — not toy demos, actual agents doing actual tasks. Sixteen gigabytes sounds like a lot until you do the math. It's a hard ceiling: it sets, very precisely, how large a model you can hold in memory and run at usable speed. Once you account for the weights and a working margin for the key/value cache, that ceiling puts you at roughly a 12-billion-parameter model, quantized to 4 bits, if you want any room to breathe.

And a quantized 12B model does not compete with a frontier model. Not on reasoning, not on long-horizon tasks, not on tool use. Any public benchmark will confirm it, and mine did too.

The obvious move at that point is to give up and rent a frontier API. I didn't want to do that, so I went looking for a different answer. What I landed on is the reason Primer exists.

## The bet

Plainly stated: a small model given the *perfect context* for a single, narrow task can approach the task accuracy of a frontier model on that same task.

That sounds like wishful thinking until you look at what attention actually does. A transformer builds a vector for each token, and self-attention updates it by mixing in information from every other token, weighted by a softmax. The model has exactly one unit of attention to spend per token, and it must spread that single unit across the entire context — the whole budget, no matter how long the context gets.

Say the answer to a question depends on five genuinely relevant tokens. In a tight 200-token context, those five get a large, clean share of that one unit — a confident, well-supported answer. In a bloated 50,000-token context, those same five now compete against 49,995 distractors. Each one only steals a sliver, but there are so many slivers that the signal thins out. This is the same mechanism behind published effects like "lost in the middle" and context rot: accuracy on a fixed question degrades as you pad the input, even when the answer is still sitting in there somewhere.

The part that keeps this from being just a hardware hack: every transformer suffers this, frontier models included. They tolerate more noise because they have more parameters and better training, but the dilution itself is structural, not a defect of small models specifically. Optimizing context is not a crutch for weak hardware. It is a lever on the whole class of models.

To be upfront: I have not run a controlled benchmark proving a decomposed fleet of 12B models matches Opus or a GPT-5-class system on some fixed suite. What I have is a mechanism I find genuinely convincing, and a platform built to go test it. That's a thesis, not a settled result, and I'd rather say so plainly than let you find out later. That candor is the whole point of writing this.

## Building it as a chain

Primer was built as a narrative: each subsystem is the answer to a problem the previous one exposed.

**Microagents.** If small models struggle with big, general jobs, stop giving them big, general jobs. Split the task into many single-purpose agents, each with its own tiny, focused context: a short system prompt and a two- or three-item tool list, nothing more. A "bug-triager" agent that turns a raw report into a clean issue doesn't need a persona, doesn't need fifty tools, and doesn't need the history of some other job. It needs the couple of file tools it will actually use and four lines of instructions. That's the unit Primer is built around.

**Tool routing.** Decomposing into many agents immediately created a new bloat source: tool definitions. A real system accumulates dozens or hundreds of tools, and importing them wholesale into every agent's context defeats the point. The fix was two meta-tools instead of many: one that semantically searches a catalog of every tool in the system given a plain-language description of what you need, and one that dispatches the chosen tool by id. Two tools in context can reach any tool that exists; the catalog lives in a vector index, not in the prompt.

**Workspaces.** Once agents are small and specialized, they still need to hand work to each other, and there's nowhere for shared state to live inside anyone's minimal context. I looked at existing agent sandboxes for an answer and found the same disease I was trying to cure — huge tool surfaces, heavy conventions, all landing right back in the context. So I stripped it to the essence: a workspace is just a shared file space and a shared process space. One agent writes a file, the next reads it; a read-before-write rule keeps them from clobbering each other. The newest thing built on this, in 0.3.0, is mounting a whole knowledge collection into a workspace as a live, editable directory — an agent reads and revises the files directly, and a diff preview syncs the edits back upstream. Knowledge stops being a read-only blob and becomes something the agent actually works on.

**Graphs.** Workspaces let agents share state, but sharing isn't sequencing — nothing says which agent runs first, or when a loop should stop. A graph is a directed graph of agent nodes, allowed to be cyclic, bounded by a max-iterations limit so a loop can't run forever. The pattern I most wanted to test was the producer/judge loop: one agent drafts something, a second critiques it and either accepts it or sends it back with specific feedback, looping until the judge is satisfied or the budget runs out. Neither agent needs to be a generalist. The loop supplies the quality a single pass can't.

**Event-driven execution.** Sequencing small agents into loops has an honest cost: work a frontier model might do in one pass is now spread across many small passes over time. A feedback loop can run for hours, and you can't reasonably hold a chat window open for that. So execution had to become event-driven instead of conversation-driven: a yielding tool can park a run — no held connection, no pinned worker — and pick it back up later when whatever it's waiting on arrives. One version waits on a human answering a question; another waits on a schedule firing. The platform trades compute for time, and this is what makes that trade survivable.

## What I'd tell someone starting today

Keep every context absurdly small on purpose — smaller than feels comfortable. Decompose further than feels necessary; a task that "obviously" needs one smart agent usually splits into three or four narrow ones, each easier to get right. Don't reach for a bigger tool catalog when a narrower one will do — search for the capability you need instead of carrying all of them around. Push state out of the model's context and into something durable — a file, a shared workspace, a queue — so the model's actual job is reasoning over a small slice, not remembering everything at once. And when a single pass isn't good enough, don't chase a smarter single pass; build a loop and let a second attempt, or a hundredth, fix what the first one got wrong. Time and structure are cheap. Context is not.

I've started calling this loop engineering: less "prompt better," more "build the machinery that lets an imperfect model reach a good answer over several small, cheap steps."

## Where this actually stands

The claim that orchestrated small models can match a frontier model on a decomposed task is a hypothesis, not a settled result. Primer is the apparatus I built to test it, not proof that it's already true. If the hypothesis holds, the same techniques should make frontier models better too, because the attention dilution they're fighting is universal. If it only half-holds, it's still a clean way to build multi-agent systems on modest hardware — which was the actual goal the day I started.

Primer is open source, self-hosted, and genuinely early. The repo is at github.com/primerhq/primer; `pipx install 'primer-ai[full]'` gets you running. Try it, point it at your own gaming GPU or a cloud box, and tell me where the thesis breaks. That's the whole ask.
