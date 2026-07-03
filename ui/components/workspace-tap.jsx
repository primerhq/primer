/* global React, Icon, Btn, Banner, relativeTime */
// WorkspaceTap — live tap consumer for workspace-level event stream.
//
// Connects to GET /v1/workspaces/{wid}/tap via EventSource (cookie auth +
// native Last-Event-ID reconnect, no custom headers needed — fits SSE GET).
// Selector is optional; when provided it is sent as ?selector=<JSON>.
//
// No-build scope rule: top-level names are prefixed WTP_ so they don't
// collide with other script-tag files that share global scope.

// ---------------------------------------------------------------------------
// Event-class colour chips
// ---------------------------------------------------------------------------

var WTP_CLASS_COLORS = {
  assistant_token:  "var(--accent)",
  tool_call:        "var(--violet)",
  tool_result:      "var(--blue)",
  yielded:          "var(--amber)",
  done:             "var(--green)",
  error:            "var(--red)",
  graph_transition: "var(--text-3)",
  user_input:       "var(--text-2)",
};

function WTP_ClassChip({ cls }) {
  var color = WTP_CLASS_COLORS[cls] || "var(--text-3)";
  return (
    <span
      className="pill"
      style={{
        color: color,
        borderColor: "var(--border)",
        background: "var(--bg-2)",
        fontFamily: "IBM Plex Mono, monospace",
        fontSize: 10,
        letterSpacing: "0.04em",
      }}
    >
      {cls || "unknown"}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Known event classes for the filter chips
// ---------------------------------------------------------------------------

var WTP_ALL_CLASSES = [
  "assistant_token",
  "tool_call",
  "tool_result",
  "yielded",
  "done",
  "error",
  "graph_transition",
  "user_input",
];

// ---------------------------------------------------------------------------
// Content-aware, per-class payload preview (compact + information-dense).
//
// Each frame gets a preview tuned to its class so the row shows the MOST useful
// info without expanding: assistant text lines, a tool's args/result, a graph
// transition, the outcome of a done/error, etc. `clampLines` varies per class
// so rows have VARIABLE height (not uniform boxes) — markers stay one line,
// content frames span a few clamped lines.
// ---------------------------------------------------------------------------

function WTP_clip(s, max) {
  if (typeof s !== "string") return "";
  var t = s.trim();
  return t.length > max ? t.slice(0, max) + "…" : t;
}

function WTP_argsPreview(obj, max) {
  if (obj == null) return "";
  if (typeof obj === "string") return WTP_clip(obj, max);
  try {
    return WTP_clip(JSON.stringify(obj), max);
  } catch (_e) {
    return "";
  }
}

// Returns { label, text, clampLines, err } for one TapEvent:
//   label      — optional bold prefix (tool name / node id)
//   text       — the payload preview (may wrap to `clampLines` lines)
//   clampLines — max preview lines before clamp (variable height per class)
//   err        — render in the error colour
function WTP_eventContent(ev) {
  var cls = ev && ev.class;
  var p = (ev && ev.payload && typeof ev.payload === "object") ? ev.payload : {};

  if (cls === "assistant_token") {
    var text = typeof p.text === "string" ? p.text
             : (typeof p.delta === "string" ? p.delta : "");
    return { label: "", text: WTP_clip(text, 400), clampLines: 3, err: false };
  }
  if (cls === "user_input") {
    var ut = p.text || p.content || p.message || "";
    return { label: "", text: WTP_clip(String(ut), 400), clampLines: 3, err: false };
  }
  if (cls === "tool_call") {
    var name = p.tool_name || p.name || "";
    var args = p.arguments != null ? p.arguments : p.args;
    return { label: name || "", text: WTP_argsPreview(args, 220), clampLines: 2, err: false };
  }
  if (cls === "tool_result") {
    var rname = p.tool_name || p.name || "";
    if (p.error) {
      return { label: rname || "", text: WTP_argsPreview(p.error, 220), clampLines: 2, err: true };
    }
    var out = p.output != null ? p.output : (p.result != null ? p.result : "");
    return { label: rname || "", text: WTP_argsPreview(out, 220), clampLines: 2, err: false };
  }
  if (cls === "graph_transition") {
    var node = p.node_id || "";
    var phase = p.phase || "";
    var status = p.status ? " (" + p.status + ")" : "";
    var trans = (phase + status).trim();
    return { label: node, text: trans, clampLines: 1, err: false };
  }
  if (cls === "done") {
    var reason = p.stop_reason || p.raw_reason || "done";
    var u = p.usage || {};
    var toks = (u.input_tokens != null || u.output_tokens != null)
      ? " · " + (u.input_tokens || 0) + " in / " + (u.output_tokens || 0) + " out"
      : "";
    return { label: "", text: reason + toks, clampLines: 1, err: false };
  }
  if (cls === "error") {
    var msg = p.message || p.error || p.detail || "error";
    var code = p.code ? " [" + p.code + "]" : "";
    return { label: "", text: WTP_clip(String(msg) + code, 240), clampLines: 2, err: true };
  }
  // yielded / resumed / cancelled / unknown: a compact generic preview.
  return { label: "", text: WTP_argsPreview(p, 160), clampLines: 1, err: false };
}

// ---------------------------------------------------------------------------
// Build selector JSON from selected class names.
// Shape:
//   { "events": { "kind": "predicate", "left": { "kind": "field", "name": "class" },
//                 "op": "in", "right": { "kind": "value", "value": [...] } } }
// Matches the Op.IN / FieldRef / Value shapes from primer/model/storage.py
// and verified against tests/api/test_workspace_tap_sse.py.
// ---------------------------------------------------------------------------

function WTP_buildSelector(selectedClasses, sessionId) {
  var eventsFilter = null;
  if (selectedClasses && selectedClasses.length === 0) {
    // All classes explicitly deselected: stream NOTHING (an unsatisfiable
    // ``class IN []``) so the server matches the empty-state hint
    // "All event classes filtered out — select at least one." A null filter
    // here would instead stream everything, contradicting the UI.
    eventsFilter = {
      kind: "predicate",
      left: { kind: "field", name: "class" },
      op: "in",
      right: { kind: "value", value: [] },
    };
  } else if (selectedClasses && selectedClasses.length > 0 && selectedClasses.length < WTP_ALL_CLASSES.length) {
    eventsFilter = {
      kind: "predicate",
      left: { kind: "field", name: "class" },
      op: "in",
      right: { kind: "value", value: selectedClasses },
    };
  }

  var sessionsFilter = null;
  if (sessionId) {
    sessionsFilter = {
      kind: "predicate",
      left: { kind: "field", name: "id" },
      op: "=",
      right: { kind: "value", value: sessionId },
    };
  }

  if (!eventsFilter && !sessionsFilter) return null;
  var sel = {};
  if (eventsFilter) sel.events = eventsFilter;
  if (sessionsFilter) sel.sessions = sessionsFilter;
  return sel;
}

// ---------------------------------------------------------------------------
// Full-event detail (expand-to-inspect). Pretty-prints the whole TapEvent —
// class, session/node ids, ts, seq, cursor, and the full payload (tool args,
// tool result, token text, transition detail) — for the expandable rows.
// ---------------------------------------------------------------------------

function WTP_detailJson(ev) {
  try {
    return JSON.stringify(ev, null, 2);
  } catch (_e) {
    try { return String(ev); } catch (_e2) { return ""; }
  }
}

// ---------------------------------------------------------------------------
// WorkspaceTap — main component
//
// Reads the SHARED workspace tap (foundation/use-workspace-tap.js): ONE
// EventSource per workspace feeds every consumer in a Studio view. Class /
// session filtering is CLIENT-side over the shared buffer, so toggling a chip
// is instant and never reconnects (previously each filter change re-opened a
// server-selectored EventSource).
// ---------------------------------------------------------------------------

function WorkspaceTap({ wid, sessionId }) {
  var tap = window.useWorkspaceTap(wid);
  var allEvents = tap.events;
  var connState = tap.connState;

  // selectedClasses: null means all; a subset array filters client-side.
  var [selectedClasses, setSelectedClasses] = React.useState(null);
  // expanded: row key -> bool. Collapsed by default; only the open row renders
  // its (potentially large) detail block, so long lists stay cheap.
  var [expanded, setExpanded] = React.useState({});

  var scrollRef = React.useRef(null);
  var stickRef = React.useRef(true);

  // Client-side filter over the shared buffer.
  var events = React.useMemo(function () {
    var out = allEvents;
    if (sessionId) {
      out = out.filter(function (ev) { return ev.session_id === sessionId; });
    }
    if (selectedClasses !== null) {
      out = out.filter(function (ev) { return selectedClasses.indexOf(ev.class) >= 0; });
    }
    return out;
  }, [allEvents, selectedClasses, sessionId]);

  // Auto-scroll stick-to-bottom
  var onScroll = React.useCallback(function() {
    var el = scrollRef.current;
    if (!el) return;
    stickRef.current = (el.scrollHeight - el.scrollTop - el.clientHeight) < 80;
  }, []);

  React.useEffect(function() {
    if (!scrollRef.current || !stickRef.current) return;
    var el = scrollRef.current;
    var raf = requestAnimationFrame(function() { el.scrollTop = el.scrollHeight; });
    return function() { cancelAnimationFrame(raf); };
  }, [events]);

  // Connection state badge
  var connBadge = connState === "live"
    ? <span className="pill pill-running" data-testid="tap-conn-live"><span className="dot"></span>live</span>
    : connState === "connecting"
      ? <span className="pill pill-paused" data-testid="tap-conn-connecting"><span className="dot"></span>connecting</span>
      : <span className="pill pill-failed" data-testid="tap-conn-error"><span className="dot"></span>error · reconnecting</span>;

  // Toggle a class in the filter
  function toggleClass(cls) {
    setSelectedClasses(function(prev) {
      // null means "all selected" — convert to explicit full set first
      var current = prev === null ? WTP_ALL_CLASSES.slice() : prev.slice();
      var idx = current.indexOf(cls);
      if (idx >= 0) {
        current.splice(idx, 1);
      } else {
        current.push(cls);
      }
      // If everything is selected, go back to null (no filter)
      if (current.length === WTP_ALL_CLASSES.length) return null;
      return current;
    });
  }

  function isClassSelected(cls) {
    return selectedClasses === null || selectedClasses.indexOf(cls) >= 0;
  }

  function selectAll() { setSelectedClasses(null); }

  function toggleExpand(key) {
    setExpanded(function (prev) {
      var next = {};
      for (var k in prev) next[k] = prev[k];
      next[key] = !prev[key];
      return next;
    });
  }

  var activeClasses = selectedClasses === null ? WTP_ALL_CLASSES : selectedClasses;

  return (
    <div className="col" style={{ gap: 0 }} data-testid="workspace-tap-root">
      {/* Filter bar */}
      <div
        style={{
          padding: "8px 14px",
          borderBottom: "1px solid var(--border)",
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          alignItems: "center",
        }}
        data-testid="tap-filter-bar"
      >
        <span className="muted text-sm" style={{ marginRight: 4 }}>Filter:</span>
        {WTP_ALL_CLASSES.map(function(cls) {
          var active = isClassSelected(cls);
          var color = WTP_CLASS_COLORS[cls] || "var(--text-3)";
          return (
            <button
              key={cls}
              type="button"
              data-testid={"tap-filter-" + cls}
              onClick={function() { toggleClass(cls); }}
              style={{
                background: active ? "var(--bg-2)" : "transparent",
                border: "1px solid " + (active ? color : "var(--border)"),
                borderRadius: 4,
                padding: "2px 8px",
                cursor: "pointer",
                color: active ? color : "var(--text-3)",
                fontFamily: "IBM Plex Mono, monospace",
                fontSize: 10,
                letterSpacing: "0.04em",
                opacity: active ? 1 : 0.5,
              }}
            >
              {cls}
            </button>
          );
        })}
        {selectedClasses !== null && (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            style={{ marginLeft: 4 }}
            onClick={selectAll}
          >all</button>
        )}
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
          <span className="muted text-sm">{events.length} event{events.length === 1 ? "" : "s"}</span>
          {connBadge}
          <Btn
            size="sm"
            kind="ghost"
            icon="trash"
            onClick={function() { tap.clear(); }}
            title="Clear event list"
          >Clear</Btn>
        </div>
      </div>

      {/* Event list */}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        style={{ overflowY: "auto", maxHeight: 520, minHeight: 120, padding: "6px 0" }}
        data-testid="tap-event-list"
      >
        {events.length === 0 && (
          <div className="muted text-sm" style={{ padding: "20px 18px", textAlign: "center" }}>
            {connState === "connecting"
              ? "Connecting to tap stream…"
              : connState === "error"
                ? "Stream error — EventSource will auto-reconnect."
                : activeClasses.length === 0
                  ? "All event classes filtered out — select at least one."
                  : "Connected. Waiting for events…"}
          </div>
        )}
        {events.map(function(ev, i) {
          var key = ev.cursor != null ? String(ev.cursor) : ("i" + i);
          var isOpen = !!expanded[key];
          var ts = ev.ts ? new Date(ev.ts) : null;
          // Minimal timestamp: HH:MM:SS (full ISO on hover).
          var tsLabel = ts ? ts.toISOString().slice(11, 19) : "—";
          var sid = ev.session_id || "";
          var sidShort = sid.length > 16 ? sid.slice(0, 8) + "…" + sid.slice(-4) : sid;
          var content = WTP_eventContent(ev);
          return (
            <div key={key} data-testid="activity-event" style={{ borderBottom: "1px solid var(--border)" }}>
              {/* Compact, content-aware row — click / Enter / Space expands it.
                  A minimal timestamp + class chip + session on the header line,
                  then a class-tuned payload preview with VARIABLE height. */}
              <div
                data-testid="tap-event-row"
                role="button"
                tabIndex={0}
                aria-expanded={isOpen}
                onClick={function () { toggleExpand(key); }}
                onKeyDown={function (e) {
                  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleExpand(key); }
                }}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 2,
                  padding: "5px 18px",
                  cursor: "pointer",
                  background: isOpen ? "var(--bg-2)" : "transparent",
                }}
              >
                {/* Header line: chevron · timestamp · class · session · seq */}
                <div style={{ display: "flex", alignItems: "baseline", gap: 8, lineHeight: 1.4 }}>
                  <span
                    aria-hidden="true"
                    className="mono muted"
                    style={{ fontSize: 9, flexShrink: 0, width: 8, color: "var(--text-4)" }}
                  >{isOpen ? "▾" : "▸"}</span>
                  <span
                    className="mono muted"
                    style={{ fontSize: 10, flexShrink: 0, minWidth: 56 }}
                    title={ts ? ts.toISOString() : ""}
                  >{tsLabel}</span>
                  <WTP_ClassChip cls={ev.class} />
                  {sid && (
                    <span
                      className="mono muted"
                      style={{ fontSize: 10, flexShrink: 0 }}
                      title={sid}
                    >{sidShort}</span>
                  )}
                  {ev.seq != null && (
                    <span className="mono muted" style={{ fontSize: 10, flexShrink: 0, marginLeft: "auto" }}>#{ev.seq}</span>
                  )}
                </div>
                {/* Payload preview — variable height, class-tuned line clamp. */}
                {(content.label || content.text) && (
                  <div
                    data-testid="tap-event-preview"
                    style={{ display: "flex", gap: 6, paddingLeft: 26, minWidth: 0 }}
                  >
                    {content.label && (
                      <span
                        className="mono"
                        style={{ fontSize: 11, fontWeight: 600, flexShrink: 0, color: content.err ? "var(--red)" : "var(--violet)" }}
                      >{content.label}</span>
                    )}
                    {content.text && (
                      <span
                        className="mono"
                        style={{
                          fontSize: 11,
                          color: content.err ? "var(--red)" : "var(--text-2)",
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                          overflow: "hidden",
                          display: "-webkit-box",
                          WebkitLineClamp: content.clampLines,
                          WebkitBoxOrient: "vertical",
                          minWidth: 0,
                        }}
                        title={content.text}
                      >{content.text}</span>
                    )}
                  </div>
                )}
              </div>
              {isOpen && (
                <div
                  data-testid="activity-event-detail"
                  style={{ padding: "2px 18px 10px 34px", background: "var(--bg-2)" }}
                >
                  <pre
                    className="mono"
                    style={{
                      margin: 0,
                      fontSize: 11,
                      lineHeight: 1.5,
                      color: "var(--text-2)",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                      maxHeight: 320,
                      overflow: "auto",
                    }}
                  >{WTP_detailJson(ev)}</pre>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

window.WorkspaceTap = WorkspaceTap;
// Explicit cross-file export (session-detail.jsx references this) instead of
// relying on global function hoisting.
window.WTP_buildSelector = WTP_buildSelector;
