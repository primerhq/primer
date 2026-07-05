/* global React */
// Session adapter (Task 11, studio-agents-interact) — maps a workspace
// Session's message stream onto the shape chat-refactor's `<Transcript>`
// already knows how to render, so a Session can be rendered through the
// reused chat UI without a second parallel renderer.
//
// SA_ = Session Adapter. No-build scope rule: top-level `var`/`function`
// declarations (mirrors ui/components/chat/transcript.jsx's own top-level
// style, not the IIFE-wrapped helper style of use-transcript.js) with
// every exported symbol assigned to `window.X` at file end.
//
// Transport rule (locked, studio-agents-interact Global Constraints): NO
// session WebSocket. History is `GET /v1/sessions/{sid}/messages`; live
// updates are the workspace tap SSE `GET /v1/workspaces/{wid}/tap`,
// scoped to this session via the same `window.WTP_buildSelector` helper
// components/workspace-tap.jsx already exports (session-scoped selector +
// TapCursor resume so the history<->live seam has no gap and no replay —
// same pattern as SessionLiveStream in components/session-detail.jsx).
//
// Two symbols are produced:
//   - SA_toTranscript(records, session): pure mapping, SessionMessageKind
//     (or the tap's mirrored TapEventClass, once normalised to the same
//     {seq, kind, payload, created_at, node_id} shape) -> the transcript
//     row shape.
//   - SA_useSessionConversation({ sid, wid }): the data hook. Its
//     `messages` field is the normalised/merged *record* stream (history
//     + live tap, deduped+sorted by seq) — callers apply SA_toTranscript
//     themselves once they also have the `session` row in hand (Task 12's
//     SessionAgentPanel renders `<Transcript>` "fed by SA_toTranscript").

// ---------------------------------------------------------------------------
// Pure mapping: SessionMessageKind -> transcript row kind
// ---------------------------------------------------------------------------

var SA_KIND_TO_TRANSCRIPT = {
  user_input: "user_message",
  assistant_token: "assistant_message",
  tool_call: "tool_call",
  tool_result: "tool_result",
  graph_transition: "divider",
  invocation_divider: "divider",
  yielded: "interaction",
  resumed: "interaction",
  done: "lifecycle",
  cancelled: "lifecycle",
  error: "lifecycle",
};

// Divider label for the two kinds SA_KIND_TO_TRANSCRIPT maps to "divider".
// invocation_divider (written by reset_session on ENDED->CREATED re-open,
// payload: {invocation: N}) renders "— invocation N —"; graph_transition
// (node ENTER/EXIT, payload: {node_id, node_kind, phase, status}) renders
// "<node_id> · <phase>".
function SA_dividerLabel(rec) {
  if (rec.kind === "invocation_divider") {
    var n = (rec.payload && rec.payload.invocation) || 1;
    return "— invocation " + n + " —";
  }
  var p = rec.payload || {};
  return (p.node_id || "node") + " · " + (p.phase || "");
}

// records: SessionMessageRecord-shaped rows — {seq, kind, payload,
// created_at, node_id}, whether loaded from the REST history endpoint or
// normalised from a live TapEvent (see SA_useSessionConversation below).
// session: the WorkspaceSession row (reserved for session-aware rendering
// decisions a future task may need — not read here yet).
function SA_toTranscript(records, session) {
  var out = [];
  for (var i = 0; i < records.length; i++) {
    var rec = records[i];
    var kind = SA_KIND_TO_TRANSCRIPT[rec.kind] || "lifecycle";
    out.push({
      seq: rec.seq,
      kind: kind,
      nodeId: rec.node_id || null,
      label: kind === "divider" ? SA_dividerLabel(rec) : undefined,
      payload: rec.payload || {},
      createdAt: rec.created_at,
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// Cursor encode — scopes the tap's resume cursor to this one session so the
// live tail picks up exactly where the REST history left off. Same shape as
// components/session-detail.jsx's private _slsEncodeCursor (TapCursor's
// {known_as_of, seqs: {sid: seq}} wire form); duplicated locally (SA_-
// prefixed) since that one isn't exported to `window`.
// ---------------------------------------------------------------------------

function SA_encodeCursor(sid, seq) {
  var payload = { known_as_of: "1970-01-01T00:00:00+00:00", seqs: {} };
  payload.seqs[sid] = seq;
  var json = JSON.stringify(payload);
  var b64;
  try {
    b64 = btoa(unescape(encodeURIComponent(json)));
  } catch (_e) {
    return null;
  }
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// ---------------------------------------------------------------------------
// SA_useSessionConversation — data hook backing a Session's live view.
// ---------------------------------------------------------------------------

function SA_useSessionConversation(opts) {
  var sid = opts && opts.sid;
  var wid = opts && opts.wid;
  var primerApi = window.primerApi || {};
  var apiFetch = primerApi.apiFetch;
  var useResource = primerApi.useResource;

  // Session row (status + turn_status) — light poll; the tap effect below
  // stops opening new connections once it reports ENDED.
  var detail = useResource(
    "session-adapter:row:" + sid,
    function (signal) {
      return apiFetch("GET", "/sessions/" + encodeURIComponent(sid), null, { signal: signal });
    },
    { pollMs: 3000, deps: [sid] }
  );
  var sessionRow = detail.data;
  var status = sessionRow ? sessionRow.status : null;
  var turnStatus = sessionRow ? sessionRow.turn_status : "idle";

  // Pending yield(s) for this session — inline interaction affordances
  // (studio-agents-interact §5.4 / §4.5's session-scoped read).
  var pendingRes = useResource(
    "session-adapter:pending:" + sid,
    function (signal) {
      if (!wid) return Promise.resolve({ items: [] });
      return apiFetch(
        "GET",
        "/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid) + "/yields/pending",
        null,
        { signal: signal }
      );
    },
    { pollMs: 4000, deps: [sid, wid] }
  );
  var pending = (pendingRes.data && pendingRes.data.items) || [];

  // Raw normalised message-log records — REST history seed + live tap,
  // merged/deduped/sorted by seq. SA_toTranscript is applied by callers
  // (they also hold the `session` row for the mapping's second argument).
  var recordsState = React.useState([]);
  var records = recordsState[0];
  var setRecords = recordsState[1];
  var historyLoadedState = React.useState(false);
  var historyLoaded = historyLoadedState[0];
  var setHistoryLoaded = historyLoadedState[1];
  var historyCursorRef = React.useRef(0);

  // History load — GET /sessions/{sid}/messages (paginated; the server
  // caps a single page at 1000, comfortably above one session's log in
  // the common case). Best-effort: a failed history fetch still lets the
  // live tap populate the stream from here on.
  React.useEffect(function () {
    var alive = true;
    setRecords([]);
    setHistoryLoaded(false);
    historyCursorRef.current = 0;
    if (!sid) return undefined;
    (function () {
      return apiFetch("GET", "/sessions/" + encodeURIComponent(sid) + "/messages?limit=1000")
        .then(function (res) {
          if (!alive) return;
          var items = (res && res.items) || [];
          var maxSeq = 0;
          for (var i = 0; i < items.length; i++) {
            if (typeof items[i].seq === "number" && items[i].seq > maxSeq) maxSeq = items[i].seq;
          }
          historyCursorRef.current = maxSeq;
          if (items.length > 0) {
            setRecords(function (prev) {
              var seen = {};
              for (var j = 0; j < prev.length; j++) seen[prev[j].seq] = true;
              var merged = prev.concat(items.filter(function (it) { return !seen[it.seq]; }));
              merged.sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); });
              return merged;
            });
          }
        })
        .catch(function () { /* history is best-effort; the live tap still tails */ })
        .then(function () { if (alive) setHistoryLoaded(true); });
    })();
    return function () { alive = false; };
  }, [sid]); // eslint-disable-line react-hooks/exhaustive-deps

  // Live tail — the session-scoped workspace tap (no session WebSocket).
  // Opens only once history has loaded so the resume cursor carries
  // history's high-water seq (no gap, no replay at the seam); skipped
  // once the session is ENDED (a terminal session has nothing left to
  // tail — the REST history above is already the full transcript).
  React.useEffect(function () {
    if (!wid || !sid || !historyLoaded) return undefined;
    if (status === "ended") return undefined;

    var selector = window.WTP_buildSelector ? window.WTP_buildSelector(null, sid) : null;
    var highWater = historyCursorRef.current || 0;
    var cursorToken = highWater > 0 ? SA_encodeCursor(sid, highWater) : null;

    var url = "/v1/workspaces/" + encodeURIComponent(wid) + "/tap";
    var params = [];
    if (selector) params.push("selector=" + encodeURIComponent(JSON.stringify(selector)));
    if (cursorToken) params.push("cursor=" + encodeURIComponent(cursorToken));
    if (params.length > 0) url += "?" + params.join("&");

    var es;
    try {
      es = new EventSource(url, { withCredentials: true });
    } catch (_e) {
      return undefined;
    }

    es.onmessage = function (ev) {
      var tev;
      try { tev = JSON.parse(ev.data); } catch (_e) { return; }
      if (!tev || typeof tev.seq !== "number") return;
      // Normalise the TapEvent (class/ts) onto the SessionMessageRecord
      // shape (kind/created_at) records already carry from REST history.
      var rec = {
        seq: tev.seq,
        kind: tev.class,
        payload: (tev.payload && typeof tev.payload === "object") ? tev.payload : {},
        created_at: tev.ts,
        node_id: tev.node_id != null ? tev.node_id : null,
      };
      setRecords(function (prev) {
        for (var i = 0; i < prev.length; i++) {
          if (prev[i].seq === rec.seq) return prev;
        }
        var next = prev.concat([rec]);
        next.sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); });
        return next;
      });
    };

    es.onerror = function () { /* EventSource reconnects natively via Last-Event-ID */ };

    return function () { try { es.close(); } catch (_e) { /* no-op */ } };
  }, [wid, sid, historyLoaded, status]); // eslint-disable-line react-hooks/exhaustive-deps

  // sendMessage/stop/end — one input, three behaviours (§5.1): a message
  // to a CREATED session invokes it, to RUNNING/WAITING it steers, to
  // PAUSED it resumes (all auto-wake, server-side). Stop preempts the
  // turn but keeps the session alive; End hard-cancels it.
  var sendMessage = React.useCallback(function (text) {
    if (!wid || !sid) return Promise.reject(new Error("sendMessage: wid and sid are required"));
    return apiFetch(
      "POST",
      "/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid) + "/steer",
      { instruction: text }
    );
  }, [apiFetch, wid, sid]);

  var stop = React.useCallback(function () {
    if (!wid || !sid) return Promise.resolve();
    return apiFetch(
      "POST",
      "/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid) + "/interrupt"
    );
  }, [apiFetch, wid, sid]);

  var end = React.useCallback(function () {
    if (!wid || !sid) return Promise.resolve();
    return apiFetch(
      "POST",
      "/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid) + "/cancel"
    );
  }, [apiFetch, wid, sid]);

  return {
    messages: records,
    status: status,
    turnStatus: turnStatus,
    sendMessage: sendMessage,
    stop: stop,
    end: end,
    pending: pending,
  };
}

window.SA_toTranscript = SA_toTranscript;
window.SA_KIND_TO_TRANSCRIPT = SA_KIND_TO_TRANSCRIPT;
window.SA_useSessionConversation = SA_useSessionConversation;
