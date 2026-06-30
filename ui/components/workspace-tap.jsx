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
// One-line payload summary
// ---------------------------------------------------------------------------

function WTP_payloadSummary(payload) {
  if (!payload || typeof payload !== "object") return "";
  // tool_call / tool_result
  if (payload.tool_name) return payload.tool_name;
  if (payload.name) return payload.name;
  // assistant_token: show up to 60 chars of text
  if (typeof payload.text === "string") {
    var t = payload.text.replace(/\n/g, " ").trim();
    return t.length > 60 ? t.slice(0, 60) + "…" : t;
  }
  // graph_transition
  if (payload.node_id) return payload.node_id + (payload.phase ? " · " + payload.phase : "");
  // yielded
  if (payload.tool) return "yielded · " + payload.tool;
  // generic: stringify up to 80 chars
  try {
    var s = JSON.stringify(payload);
    return s.length > 80 ? s.slice(0, 80) + "…" : s;
  } catch (_) {
    return "";
  }
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
  if (selectedClasses && selectedClasses.length > 0 && selectedClasses.length < WTP_ALL_CLASSES.length) {
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
// WorkspaceTap — main component
// ---------------------------------------------------------------------------

var WTP_MAX_EVENTS = 500;

function WorkspaceTap({ wid, sessionId }) {
  // selectedClasses: null means all; empty array (de-selected all) = all too.
  // We initialise to null (all), and null means no filter is added.
  var [events, setEvents] = React.useState([]);
  var [connState, setConnState] = React.useState("connecting");
  var [selectedClasses, setSelectedClasses] = React.useState(null);

  // Derived selector
  var selector = React.useMemo(
    function() { return WTP_buildSelector(selectedClasses, sessionId || null); },
    [selectedClasses, sessionId]
  );

  var scrollRef = React.useRef(null);
  var stickRef = React.useRef(true);
  var esRef = React.useRef(null);

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

  // EventSource lifecycle — re-opens when wid, selector, or sessionId changes
  React.useEffect(function() {
    if (!wid) return;

    var url = "/v1/workspaces/" + encodeURIComponent(wid) + "/tap";
    if (selector) {
      url += "?selector=" + encodeURIComponent(JSON.stringify(selector));
    }

    var es = new EventSource(url, { withCredentials: true });
    esRef.current = es;
    setConnState("connecting");
    setEvents([]);

    es.onopen = function() {
      setConnState("live");
    };

    es.onmessage = function(e) {
      var ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      if (!ev || typeof ev !== "object") return;
      setEvents(function(prev) {
        var next = prev.concat(ev);
        if (next.length > WTP_MAX_EVENTS) next = next.slice(next.length - WTP_MAX_EVENTS);
        return next;
      });
    };

    es.onerror = function() {
      // EventSource handles reconnect natively via Last-Event-ID.
      // We just update the indicator; onerror fires on temporary drops too.
      setConnState("error");
    };

    return function() {
      es.close();
      esRef.current = null;
    };
  }, [wid, selector]); // eslint-disable-line react-hooks/exhaustive-deps

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
            onClick={function() { setEvents([]); }}
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
          var ts = ev.ts ? new Date(ev.ts) : null;
          var tsLabel = ts ? ts.toISOString().slice(11, 23) : "—";
          var sid = ev.session_id || "";
          var sidShort = sid.length > 16 ? sid.slice(0, 8) + "…" + sid.slice(-4) : sid;
          var summary = WTP_payloadSummary(ev.payload);
          return (
            <div
              key={ev.cursor != null ? ev.cursor : i}
              data-testid="tap-event-row"
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: 8,
                padding: "4px 18px",
                borderBottom: "1px solid var(--border)",
                fontSize: 12,
                lineHeight: 1.5,
              }}
            >
              <span
                className="mono muted"
                style={{ fontSize: 10, flexShrink: 0, minWidth: 80 }}
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
              {summary && (
                <span
                  className="mono"
                  style={{ fontSize: 11, color: "var(--text-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}
                  title={summary}
                >{summary}</span>
              )}
              {ev.seq != null && (
                <span className="mono muted" style={{ fontSize: 10, flexShrink: 0 }}>#{ev.seq}</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

window.WorkspaceTap = WorkspaceTap;
