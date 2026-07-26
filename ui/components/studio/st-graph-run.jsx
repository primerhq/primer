/* global React, ST2_api */
// Studio revamp - the graph run screen (ui/studio/STUDIO-WIRING.md §10).
//
// The superstep strip is DOM chrome ABOVE the canvas, never a second canvas:
// GR_Canvas already owns node rendering, and the mockup's absolutely-positioned
// cards are a drawing of that, not an instruction to reimplement it. This module
// contributes the strip, the lane routing, and nothing else.
//
// SUPERSTEPS ARE DERIVED. WIRING §10 says "one segment per superstep", but
// node_states (compute.py:234 _NodeStateOut) carries no superstep field - the
// fields are node_id, kind, status, iteration, last_run_at, tokens_in,
// tokens_out, duration_ms, error. `iteration` IS the superstep index
// (last_run_iteration), so the grouping is derived from it. Nodes that have not
// run carry no iteration at all and cannot be placed in a numbered superstep;
// they collect into a trailing "upcoming" segment rather than being silently
// dropped (which would make the strip claim a graph is smaller than it is).

// Node statuses are pending|running|completed|failed|skipped - NOT session
// statuses, so ST2_bucketOf does not apply. Nodes also have a state sessions do
// not: not started yet. It stays distinct because folding it into "working"
// would paint an idle segment as live, which is the one thing the strip exists
// to tell you.
function ST2_nodeBucket(status) {
  var s = String(status || "pending");
  if (s === "failed") return "broken";
  if (s === "running") return "working";
  // A skipped node is resolved, not pending: its branch condition excluded it.
  if (s === "completed" || s === "skipped") return "done";
  return "pending";
}

var ST2_NODE_TONE = {
  broken: "--red",
  working: "--blue",
  done: "--green",
  pending: "--border-strong",
};

// A superstep is as bad as its worst node: one failure defines the segment even
// if nine siblings completed, because that is what the operator has to act on.
var ST2_STEP_PRECEDENCE = ["broken", "working", "pending", "done"];

function ST2_stepBucket(nodes) {
  var present = {};
  (nodes || []).forEach(function (n) { present[ST2_nodeBucket(n.status)] = true; });
  for (var i = 0; i < ST2_STEP_PRECEDENCE.length; i++) {
    if (present[ST2_STEP_PRECEDENCE[i]]) return ST2_STEP_PRECEDENCE[i];
  }
  return "pending";
}

// ST2_supersteps(items) -> [{ index, nodes, bucket, tone, width, label, upcoming }]
//   `width` is the fan-out width k (how many nodes ran in that superstep).
//   `upcoming` marks the trailing not-yet-run group, which has no index.
function ST2_supersteps(items) {
  var byIter = {};
  var order = [];
  var upcoming = [];

  (items || []).forEach(function (n) {
    var it = n && n.iteration;
    if (it == null) { upcoming.push(n); return; }
    var key = String(it);
    if (!byIter[key]) { byIter[key] = []; order.push(it); }
    byIter[key].push(n);
  });

  order.sort(function (a, b) { return a - b; });

  var out = order.map(function (it) {
    var nodes = byIter[String(it)];
    var bucket = ST2_stepBucket(nodes);
    return {
      index: it,
      nodes: nodes,
      bucket: bucket,
      tone: ST2_NODE_TONE[bucket],
      width: nodes.length,
      label: ST2_stepLabel(nodes),
      upcoming: false,
    };
  });

  if (upcoming.length) {
    out.push({
      index: null,
      nodes: upcoming,
      bucket: "pending",
      tone: ST2_NODE_TONE.pending,
      width: upcoming.length,
      label: "upcoming",
      upcoming: true,
    });
  }
  return out;
}

// One node names the segment; a fan-out is named by its shared node id.
function ST2_stepLabel(nodes) {
  if (!nodes || !nodes.length) return "";
  if (nodes.length === 1) return String(nodes[0].node_id || nodes[0].kind || "node");
  var ids = {};
  nodes.forEach(function (n) { ids[n.node_id] = true; });
  var keys = Object.keys(ids);
  return keys.length === 1 ? keys[0] : keys.length + " nodes";
}

// The caption WIRING asks for: `n · label ×k`, with the multiplier only when the
// step actually fanned out.
function ST2_stepCaption(step) {
  var head = (step.index == null ? "" : step.index + " ") + step.label;
  return step.width > 1 ? head + " x" + step.width : head;
}

// A skipped node was excluded by a branch condition. Say which, when the run
// recorded it; never invent one.
function ST2_skipReason(node) {
  var n = node || {};
  if (ST2_nodeBucket(n.status) !== "done" || n.status !== "skipped") return null;
  return n.error ? "condition false - " + n.error : "condition false";
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function ST2_SuperstepStrip({ steps, activeIndex, onPick }) {
  if (!steps || !steps.length) return null;
  return (
    <div
      data-testid="superstep-strip"
      className="row"
      style={{
        height: 34, flex: "0 0 auto", gap: 3, alignItems: "stretch",
        padding: "5px 10px", borderBottom: "1px solid var(--border)",
        background: "var(--bg-2)",
      }}
    >
      {steps.map(function (step, i) {
        var live = step.bucket === "working";
        var selected = activeIndex != null && step.index === activeIndex;
        return (
          <div
            key={step.index == null ? "upcoming" : step.index}
            data-testid={step.upcoming ? "superstep-upcoming" : "superstep-" + step.index}
            data-bucket={step.bucket}
            title={ST2_stepCaption(step)}
            onClick={function () { if (onPick) onPick(step); }}
            // Weighted by node count, so a wide fan-out reads as the bulk of
            // the work it is.
            style={{
              flex: step.width + " 1 0", minWidth: 26, cursor: onPick ? "pointer" : "default",
              borderRadius: 5, overflow: "hidden", position: "relative",
              display: "flex", alignItems: "center", justifyContent: "center",
              background: "var(" + step.tone + "-dim, var(--bg-1))",
              border: "1px solid " + (selected ? "var(--text-3)" : "var(" + step.tone + ")"),
              opacity: step.upcoming ? 0.55 : 1,
            }}
          >
            {live ? (
              <span
                data-testid="superstep-sweep"
                className="st-sweep"
                style={{ position: "absolute", inset: 0 }}
              />
            ) : null}
            <span
              className="mono"
              style={{
                fontSize: "var(--fs-11)", color: "var(" + step.tone + ")",
                whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                padding: "0 5px", position: "relative",
              }}
            >{ST2_stepCaption(step)}</span>
          </div>
        );
      })}
    </div>
  );
}

// GraphRunPanel: the strip + whatever canvas the caller renders, plus lane
// routing into the companion pane. It deliberately does NOT re-render the run
// view; SessionGraphPanel keeps owning that, and this wraps it.
// Owns the node_states fetch. Split out so the hook is only ever mounted with a
// real gid/rid: useResource has no null-key guard, so a null key would compose
// to the string "null", create a cache entry, and fire a request at
// /graphs/null/runs/null/node_states.
function ST2_RunSteps({ gid, rid, onPickNode }) {
  var res = window.primerApi.useResource(
    ST2_api.keys.nodeStates(gid, rid),
    function (signal) { return ST2_api.nodeStates(gid, rid, signal); },
    { pollMs: 4000, deps: [gid, rid] }
  );
  var steps = React.useMemo(
    function () { return ST2_supersteps((res.data && res.data.items) || []); },
    [res.data]
  );
  return (
    <ST2_SuperstepStrip
      steps={steps}
      onPick={function (step) {
        // Selecting a step selects its first node, which is what the lane
        // inspector keys off.
        if (onPickNode && step.nodes.length) onPickNode(step.nodes[0].node_id);
      }}
    />
  );
}

function GraphRunPanel({ wid, gid, rid, studio, children, onPickNode }) {
  return (
    <div className="col" data-testid="graph-run-panel" style={{ flex: 1, minHeight: 0, gap: 0 }}>
      {gid && rid ? <ST2_RunSteps gid={gid} rid={rid} onPickNode={onPickNode} /> : null}
      <div style={{ flex: 1, minHeight: 0 }}>{children}</div>
    </div>
  );
}

// ST2_openLane: the lane inspector is the companion pane, not a drawer (§10).
// Falls back to the caller's in-place filter when the pane does not exist,
// which is what keeps the v1 shell working.
function ST2_openLane(studio, sid, nodeId, onFallback) {
  if (studio && typeof studio.openAside === "function" && nodeId) {
    studio.openAside({
      id: "session:" + sid,
      kind: "session",
      ref: sid,
      title: nodeId,
      glyph: "*",
      node: nodeId,
    });
    return true;
  }
  if (onFallback) onFallback(nodeId);
  return false;
}

window.GraphRunPanel = GraphRunPanel;
window.ST2_SuperstepStrip = ST2_SuperstepStrip;
window.ST2_supersteps = ST2_supersteps;
window.ST2_nodeBucket = ST2_nodeBucket;
window.ST2_stepBucket = ST2_stepBucket;
window.ST2_stepCaption = ST2_stepCaption;
window.ST2_stepLabel = ST2_stepLabel;
window.ST2_skipReason = ST2_skipReason;
window.ST2_openLane = ST2_openLane;
