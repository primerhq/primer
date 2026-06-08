---
slug: graphs
title: Graphs
section: features
summary: Build and run a multi-node agent graph in the console -- design the canvas, add nodes and edges, wire conditional branches, and run the pipeline.
---

## Overview

A graph wires several agents (or several invocations of the same agent) into
a multi-step workflow. The console provides a visual canvas where you drag
nodes onto a grid, draw edges between them, configure each node in a side
panel, and save the result. Running the graph executes every node in
dependency order, persisting per-node state to the workspace's git-backed
`.state/graphs/` tree.

Use a graph when the work splits cleanly into stages -- research, critique,
write; or fetch, classify, dispatch. Use a single agent when the work is one
continuous conversation.

## Open the graph list

Go to **Graphs** in the left nav. Each row shows the graph id, description,
node and edge counts, entry node, and a status pill (ok / N issues) fetched
from the graph validator.

Click a row to open the graph detail view with the canvas editor and the
status panel.

## Create a graph

1. Click **New graph** in the filter bar.
2. In the modal:
   - **ID** (optional) -- a slug like `incident-pipeline`. The backend
     generates one if left blank.
   - **Description** -- a short label for the list view.
   - **Seed agent** -- the agent that will occupy the first worker node. The
     console seeds a minimal valid skeleton: `Begin -> agent -> End`.
3. Click **Create**. The console opens the graph canvas.

If you have no agents yet, click **New** next to the agent dropdown to create
one inline without leaving the dialog.

## The canvas

```embed:graph-canvas
```

The canvas shows nodes on a dot-grid background. Edges are drawn as arrows
between nodes:

- Solid arrows -- static edges (always followed).
- Dashed arrows -- conditional edges (followed when a branch condition is met)
  or implicit fan-out wiring (FanOut specs, shown for reference only).

A legend at the bottom-left identifies the edge styles. Drag any node to
reposition it; the canvas auto-snaps to an 8-pixel grid. Click **Auto-layout**
in the toolbar to reset all positions to a computed left-to-right arrangement.

Click the canvas background to deselect the current node or edge.

## Node kinds

| Kind | Purpose |
|---|---|
| Begin | Entry point; receives the initial run input. Every graph needs exactly one. |
| Agent | Invokes an agent with a configurable prompt and toolset. The main workhorse. |
| End | Accepts final output; at least one End reachable from Begin is required. |
| Fan-out | Dispatches the current state to multiple downstream agent nodes in parallel. |
| Fan-in | Waits for parallel branches to finish and aggregates their outputs. |
| Tool call | Calls a platform tool directly without spinning up an agent turn. |

## Add and configure a node

1. Click **Add node** in the toolbar and pick a kind from the dropdown.
2. The new node appears on the canvas and is immediately selected.
3. The right-hand side panel opens for that node. Fill in the fields:
   - **Agent node** -- pick an agent from the dropdown, or click **+ New**
     to create one inline. Optionally override the prompt.
   - **End node** -- set the output template (may be left blank).
   - **Fan-out node** -- add one or more specs (broadcast, map, or tee),
     each pointing to a target node id.
   - **Fan-in node** -- set the aggregate template.
   - **Tool call node** -- pick a tool id from the catalogue and supply the
     arguments as JSON.
4. To rename a node, edit the **ID** field in the side panel. All edges
   referencing that node are updated automatically.
5. To delete a node, click **Delete node** in the side panel. All edges
   touching the node are removed.

## Wire edges

1. Click **Add edge** in the toolbar. The cursor changes to a crosshair and
   a prompt appears: "Click source node...".
2. Click the source node. The prompt changes to "Pick target for
   `<node-id>`...".
3. Click the target node. The edge is drawn.

Before clicking Add edge, pick **Static** or **Conditional** from the
segment control to the right of the button:

- **Static** -- the edge is always followed.
- **Conditional** -- the edge carries a JSON-path router with one or more
  branches and a default target. Configure branches in the side panel after
  drawing the edge.

To remove an edge, click it on the canvas to select it, then click **Delete
edge** in the side panel.

```callout:warning
FanOut nodes must not have outgoing edges in the edge list. Their downstream
targets are configured through the node's specs (broadcast, map, tee) and
are shown on the canvas as dashed implicit arrows. Adding a real edge from a
FanOut node is a hard violation and blocks Save.
```

## Validate and save

The canvas runs local topology checks as you edit. Violations appear in a
banner below the toolbar:

- **Hard violations** (red) -- block Save. Examples: missing Begin, End not
  reachable from Begin, duplicate node ids, broken edge endpoints, FanOut
  with outgoing edges.
- **Warnings** (amber) -- do not block Save. Examples: orphan nodes with no
  incoming edges, ToolCall referencing a tool not in the catalogue.

Fix hard violations before saving. When the banner is clear (or shows only
warnings), click **Save** in the toolbar. An "unsaved changes" label appears
whenever the draft differs from the last saved version.

Click **Discard** to revert the canvas to the last saved state.

## Run a graph

A graph run is started by creating a session bound to the graph. Go to
**Sessions**, create a new session, and select this graph as the target. The
graph executor runs each node in turn (or in parallel for fan-out branches),
persisting per-node state to the workspace's `.state/graphs/<session_id>/`
git repo. The session detail view shows the graph run progress.

```callout:warning
Pick a **local** workspace for a graph run. Because the executor persists
per-node state to the workspace's git-backed state repository, graph-bound
sessions currently require the local workspace backend; container and
kubernetes workspaces are not yet supported for graphs.
```

## Delete a graph

Open the graph detail view and click **Delete** in the status panel. A
confirmation modal appears. Deletion removes the graph definition. Sessions
that were bound to the graph are retained as historical records.

```ref:reference/api-graphs
Automate this -- the API reference covers creating, updating, and validating
graphs, and starting graph runs programmatically.
```
