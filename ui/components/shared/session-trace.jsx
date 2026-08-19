/* global React, Icon */

// SessionTracePanel - the read-side Trace view for one session turn.
//
// Reads GET /v1/sessions/{sid}/turns/{n}/timeline, which folds the
// session's own on-disk records into a tree (no trace system exists; the
// record IS the trace). Shell-agnostic on purpose: it takes everything it
// needs as props and names no route or address itself, so S8's fresh
// shell re-hosts it unchanged.

function ST_fmtMs(ms) {
  if (ms == null) return "-";
  if (ms < 1000) return ms + "ms";
  return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + "s";
}

function ST_TraceNode({ node, depth }) {
  var pad = 10 + depth * 16;
  var label;
  var meta = null;
  if (node.kind === "llm_call") {
    label = node.model || node.profile_id || "model call";
    meta = (
      <span className="mono" style={{ color: "var(--text-4)", fontSize: 11 }}>
        {node.profile_id} · in {node.input_tokens == null ? "-" : node.input_tokens}
        {" "}· out {node.output_tokens == null ? "-" : node.output_tokens}
        {" "}· {ST_fmtMs(node.duration_ms)}
      </span>
    );
  } else if (node.kind === "tool_call") {
    label = node.name || node.tool_call_id || "tool";
    meta = (
      <span className="mono" style={{ color: "var(--text-4)", fontSize: 11 }}>
        {node.status || "pending"} · {ST_fmtMs(node.duration_ms)}
      </span>
    );
  } else {
    label = node.node_id || "node";
    meta = (
      <span className="mono" style={{ color: "var(--text-4)", fontSize: 11 }}>
        {node.status || "running"} · {ST_fmtMs(node.duration_ms)}
      </span>
    );
  }
  var tint =
    node.kind === "llm_call" ? "var(--accent)"
    : node.kind === "tool_call" ? "var(--text-2)"
    : "var(--amber)";
  return (
    <div>
      <div
        data-testid={"trace-row-" + node.kind}
        style={{
          display: "flex", alignItems: "center", gap: 8,
          padding: "4px 10px", paddingLeft: pad,
        }}
      >
        <span style={{ color: tint, flexShrink: 0 }}>
          {node.kind === "llm_call" ? "◆" : node.kind === "tool_call" ? "▸" : "▣"}
        </span>
        <span className="mono" style={{ fontSize: 12, color: "var(--text)" }}>
          {label}
        </span>
        <div style={{ flex: 1 }} />
        {meta}
      </div>
      {(node.children || []).map(function (child, i) {
        return (
          <ST_TraceNode
            key={child.seq != null ? child.seq : i}
            node={child}
            depth={depth + 1}
          />
        );
      })}
    </div>
  );
}

function SessionTracePanel({ sid, turnNo, sessionStatus }) {
  var api = window.primerApi;
  var useResource = api.useResource;
  var apiFetch = api.apiFetch;
  var terminal = !!(window.SESSION_TERMINAL && window.SESSION_TERMINAL.has(sessionStatus));

  var trace = useResource(
    "session-trace:" + sid + ":" + turnNo,
    function (signal) {
      return apiFetch(
        "GET",
        "/sessions/" + encodeURIComponent(sid) + "/turns/"
          + encodeURIComponent(turnNo) + "/timeline",
        null,
        { signal }
      );
    },
    { pollMs: terminal ? 0 : 4000, deps: [sid, turnNo, sessionStatus] }
  );

  var data = trace.data;

  return (
    <div data-testid="session-trace" className="panel">
      <div className="panel-h">
        {/* "clock" is a real name in the Icon switch
            (ui/components/shared.jsx); an unknown name silently falls
            through to the plain-circle default case. */}
        <Icon name="clock" size={13} />
        <span>Trace</span>
        <div style={{ flex: 1 }} />
        {data && (
          <span className="mono" style={{ fontSize: 11, color: "var(--text-4)" }}>
            turn {data.turn_no} · {data.status} · {ST_fmtMs(data.duration_ms)}
          </span>
        )}
      </div>
      <div className="panel-body" style={{ padding: 0 }}>
        {trace.loading && !data && (
          <div className="muted text-sm" style={{ padding: 10 }}>Loading trace...</div>
        )}
        {!trace.loading && !data && (
          <div className="muted text-sm" style={{ padding: 10 }}>
            No trace for this turn yet.
          </div>
        )}
        {data && (data.waits || []).length > 0 && (
          <div
            data-testid="trace-waits"
            className="mono"
            style={{
              padding: "4px 10px", fontSize: 11, color: "var(--amber)",
              borderBottom: "1px solid var(--border)",
            }}
          >
            waited {ST_fmtMs(data.waits[0].ms)}
            {data.waits[0].event_key ? " on " + data.waits[0].event_key : ""}
          </div>
        )}
        {data && (data.children || []).length === 0 && (
          <div className="muted text-sm" style={{ padding: 10 }}>
            This turn made no model or tool calls.
          </div>
        )}
        {data && (data.children || []).map(function (child, i) {
          return (
            <ST_TraceNode
              key={child.seq != null ? child.seq : i}
              node={child}
              depth={0}
            />
          );
        })}
      </div>
    </div>
  );
}

window.SessionTracePanel = SessionTracePanel;
window.ST_TraceNode = ST_TraceNode;
