---
slug: multi-agent-graph-research
title: Multi-agent research pipeline
section: cookbook
summary: A three-node graph - researcher, fact-checker, writer - producing reviewed reports from an open question.
difficulty: advanced
time_minutes: 60
tags: [graphs, agents, knowledge]
---

## Goal

A three-node graph turns an open research question into a single
reviewed report. The researcher finds sources, the fact-checker
validates them against an internal knowledge collection, and the
writer composes the final report. A conditional back-edge between
the fact-checker and researcher handles failed sources by
re-querying with the bad sources excluded.

## Prerequisites

- Three agents created in the console: `researcher`, `fact-checker`,
  and `writer`.
- A knowledge collection named `internal-knowledge` populated with
  known-good reference material.

```ref:features/graphs
Build the canvas, add nodes and edges, wire conditional branches,
and run the pipeline.
```

```ref:features/agents-advanced
Fine-grained tool binding and the turn loop -- relevant for
scoping each agent to only the tools it needs.
```

```ref:features/knowledge-collections
Create and populate the internal knowledge collection the
fact-checker searches.
```

## Steps

### 1. Create the three agents

Open **Agents** and create each agent in turn.

**researcher**

1. Click **New agent**, ID `researcher`.
2. Pick your largest model (the researcher faces open-ended queries).
3. On the **Tools** tab, select web search and system tools.
4. System prompt (Advanced tab): "Find authoritative sources for the
   question. Return a list of source URLs with short summaries."
5. Click **Create**.

**fact-checker**

1. Click **New agent**, ID `fact-checker`.
2. A mid-tier model is sufficient for structured comparison.
3. On the **Tools** tab, select system tools with knowledge search.
4. System prompt: "For each source in the input, search
   internal-knowledge for contradictions. Return a JSON object with
   keys good_sources and bad_sources."
5. Click **Create**.

**writer**

1. Click **New agent**, ID `writer`.
2. Pick a model suited to long-form prose.
3. On the **Tools** tab, select system tools only.
4. System prompt: "Write a 500-word report citing the validated
   sources passed in. Use plain Markdown."
5. Click **Create**.

### 2. Create the graph

1. Open **Graphs** and click **New graph**.
2. Set ID to `research-pipeline`, description to "researcher
   fact-checker writer", seed agent `researcher`.
3. Click **Create**. The console opens the canvas.

```embed:graph-canvas
```

### 3. Add nodes

The canvas starts with a `Begin -> researcher -> End` skeleton.

1. Click **Add node** and add a second **Agent** node; select
   `fact-checker`. The canvas places it to the right.
2. Click **Add node** again and add a third **Agent** node; select
   `writer`.

### 4. Wire edges

Wire the static path first:

1. Click **Add edge**, keep it as **Static**, then click
   `researcher` then `fact-checker`.
2. Click **Add edge**, keep it as **Static**, then click
   `fact-checker` then `writer`.
3. Click the existing edge from `researcher` to `End` and delete it
   (select the edge, then **Delete edge** in the side panel).
4. Wire `writer` to `End` with a **Static** edge.

Now add the conditional back-edge:

5. Click **Add edge**, switch to **Conditional**, then click
   `fact-checker` then `researcher`.
6. In the side panel, configure the branch condition so the edge is
   followed when `bad_sources` is non-empty. Set the default target
   to `writer` (followed when all sources are valid).

```callout:warning
The fact-checker to researcher back-edge can loop indefinitely if
no good sources exist. Set a max-iterations limit on the graph via
the graph settings panel. The graph executor stops the run when the
limit is reached rather than consuming the full session budget.
```

### 5. Validate and save

Check the validation banner below the toolbar. Fix any hard
violations (red) before saving. Click **Save**.

### 6. Run the graph

1. Open **Sessions** and click **New session**.
2. Select a workspace and the `research-pipeline` graph as the
   target.
3. In the input field, type the research question, for example:
   "How did the SLO methodology evolve from 2018 to 2025?"
4. Click **Start**.

The graph executor runs `researcher`, then `fact-checker`, then
(conditionally) either back to `researcher` or forward to `writer`.

## Verification

Open the session in **Sessions**. The detail view shows the graph
run progress. Click into each node's session link to read the
transcript for that step.

```embed:session-detail
```

The **Log** tab on the workspace detail page shows a git commit per
node, so you can diff each stage's output against the previous one.

The final writer node produces a Markdown report as its last
assistant turn. Copy it from the transcript or retrieve it via the
sessions API.

## Gotchas

```callout:tip
Pin each agent to a fixture set in eval mode before promoting the
graph to production. Drift in the researcher's output format breaks
the fact-checker's JSON parsing, which stalls the whole pipeline.
```

- The fact-checker reads the `internal-knowledge` collection;
  populate it with known-good reference material before the first
  run or the validator has nothing to check against.
- Long-running graph runs hold workspace slots for the duration. If
  the writer step is slow, consider a longer TTL on the workspace
  template or split the writer into a separate session triggered
  by the graph's output.
- The conditional back-edge targets `researcher` with the bad
  sources excluded. Ensure the researcher's system prompt instructs
  it to treat the excluded list as off-limits, or it will propose
  the same sources again.

## Automate it

```ref:reference/api-graphs
Create and run graphs programmatically, including conditional edge
configuration and graph run status polling.
```
