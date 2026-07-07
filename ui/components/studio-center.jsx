/* global React, Icon, Btn, Banner */
// StudioCenter — center region of the Studio IDE shell (PR-B / B3).
//
// Replaces the ST_RegionPlaceholder at data-testid="region-center" inside
// the shell's data-testid="studio-center" column. Renders:
//   CenterTabs        — the tab bar over studio.state.openTabs (glyph/title,
//                        dirty dot, close ×; horizontal overflow; empty state).
//   the active panel  — chosen by the active tab's kind:
//     session(agent) → SessionAgentPanel  (header + End/Restart + Transcript/Composer)
//     session(graph) → SessionGraphPanel  (header + Pause/Cancel/Restart +
//                       a toggleable SD_GraphRunView over the SAME
//                       session-backed Transcript/Composer, Task 13)
//     file           → FilePanel          (preview/edit toggle + Save + 412 flow)
//
// REUSE (do NOT rebuild): the graph run-view is still the production
// component from session-detail.jsx; the agent transcript (Task 12,
// studio-agents-interact plan) and the graph panel's bottom transcript
// (Task 13, this file) are both chat-refactor's own reused primitives over
// the session adapter — all reached across files as the no-build window
// globals:
//   window.SD_GraphRunView        ({ gid, rid, wid, session, pushToast })
//   window.SA_useSessionConversation ({ sid, wid })  — session-adapter.jsx
//   window.SA_toTranscript           (records, session)
//   window.Transcript / window.Composer              — chat-refactor
// Markdown preview reuses window.renderMarkdown; code highlight reuses
// window.primerVendor.highlightPython.
// No-build rules: top-level declarations use `var`; helpers prefixed ST_;
// every exported symbol is assigned to window.X at the bottom.

// ---------------------------------------------------------------------------
// ST_basename — last path segment, for the file breadcrumb / tab title.
// ---------------------------------------------------------------------------

function ST_basename(path) {
  if (!path) return "";
  var parts = String(path).split("/");
  return parts[parts.length - 1] || path;
}

// ---------------------------------------------------------------------------
// ST_fileClassOf — coarse render-class for a path: "markdown"|"image"|"code"|"text".
// Drives the preview branch + whether syntax highlighting applies.
// ---------------------------------------------------------------------------

var ST_IMAGE_EXTS = { png: 1, jpg: 1, jpeg: 1, gif: 1, svg: 1, webp: 1, bmp: 1, ico: 1, avif: 1 };
var ST_MD_EXTS = { md: 1, markdown: 1, mdx: 1 };
var ST_CODE_LANGS = {
  py: "python", pyi: "python",
  js: "javascript", jsx: "javascript", ts: "javascript", tsx: "javascript",
  json: "json",
  sh: "bash", bash: "bash", zsh: "bash",
  yaml: "yaml", yml: "yaml", toml: "toml", ini: "ini",
  css: "css", html: "html", sql: "sql", rs: "rust", go: "go", c: "c",
};

function ST_extOf(path) {
  var name = ST_basename(path).toLowerCase();
  var dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1) : "";
}

function ST_fileClassOf(path) {
  var ext = ST_extOf(path);
  if (ST_MD_EXTS[ext]) return "markdown";
  if (ST_IMAGE_EXTS[ext]) return "image";
  if (ST_CODE_LANGS[ext]) return "code";
  return "text";
}

function ST_fmtBytes(n) {
  if (typeof n !== "number" || !isFinite(n)) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

// Files larger than this are preview/download-only (no edit toggle).
var ST_EDIT_MAX_BYTES = 1024 * 1024;

// ---------------------------------------------------------------------------
// CenterTabs — the tab bar over studio.state.openTabs.
//   Each tab: glyph + title + dirty ● (files) + close ×.
//   Active = activeTabId; click → focusTab; × → closeTab (confirm if dirty file).
//   Horizontal overflow scrolls (.st-tabbar { overflow-x:auto }). Empty state
//   shown when there are no open tabs.
// ---------------------------------------------------------------------------

function CenterTabs({ openTabs, activeTabId, onFocus, onClose, onCloseAll }) {
  var tabs = openTabs || [];

  function handleCloseAll(e) {
    e.stopPropagation();
    if (!tabs.length) return; // no-op when there are no open tabs
    // If any file tab has unsaved edits, confirm once before dropping them all.
    var anyDirty = tabs.some(function (t) { return t.kind === "file" && t.dirty; });
    if (!anyDirty) {
      onCloseAll && onCloseAll();
      return;
    }
    confirmDialog({
      title: "Close all tabs?",
      message: "Some files have unsaved changes. Close all tabs without saving?",
      confirmLabel: "Close all",
      danger: true,
    }).then(function (ok) {
      if (ok) onCloseAll && onCloseAll();
    });
  }

  if (tabs.length === 0) {
    return (
      <div className="st-tabbar" data-testid="center-tabs">
        <div
          style={{
            display: "flex",
            alignItems: "center",
            padding: "0 14px",
            fontSize: 11.5,
            color: "var(--text-4)",
            fontStyle: "italic",
          }}
          data-testid="center-tabs-empty"
        >
          No open tabs — pick a session or file from the sidebar.
        </div>
      </div>
    );
  }

  function handleClose(e, tab) {
    e.stopPropagation();
    // A dirty file tab carries unsaved edits — confirm before discarding.
    if (tab.kind === "file" && tab.dirty) {
      confirmDialog({
        title: "Close tab?",
        message: "“" + (tab.title || tab.ref) + "” has unsaved changes. Close without saving?",
        confirmLabel: "Close",
        danger: true,
      }).then(function (ok) {
        if (ok) onClose(tab.id);
      });
      return;
    }
    onClose(tab.id);
  }

  return (
    <div className="st-tabbar" data-testid="center-tabs">
      {tabs.map(function (tab) {
        var isActive = tab.id === activeTabId;
        return (
          <div
            key={tab.id}
            className={"st-tab" + (isActive ? " is-active" : "")}
            data-testid="center-tab"
            data-tab-id={tab.id}
            data-active={isActive ? "true" : "false"}
            title={tab.ref || tab.title}
            onClick={function () { onFocus(tab.id); }}
          >
            {tab.glyph && (
              <span style={{ color: isActive ? "var(--accent)" : "var(--text-4)", flexShrink: 0 }}>
                {tab.glyph}
              </span>
            )}
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {tab.title || ST_basename(tab.ref)}
            </span>
            {tab.kind === "file" && tab.dirty && (
              <span
                data-testid="tab-dirty"
                style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--blue)", flexShrink: 0 }}
              />
            )}
            <span
              className="st-tab-close"
              data-testid="center-tab-close"
              title="Close"
              onClick={function (e) { handleClose(e, tab); }}
            >×</span>
          </div>
        );
      })}
      {/* Close-all control — pinned to the right edge so it stays reachable
          even when the tab strip overflows and scrolls. */}
      <button
        className="st-tabs-close-all"
        data-testid="tabs-close-all"
        title="Close all tabs"
        aria-label="Close all tabs"
        onClick={handleCloseAll}
      >
        <Icon name="x" size={13} />
        <span style={{ fontSize: 11 }}>Close all</span>
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ST_TokenMeterInline — compact token meter for the agent header. Reuses the
// shared window.TokenMeter when present; degrades to a plain count otherwise.
// ---------------------------------------------------------------------------

function ST_TokenMeterInline({ session }) {
  var turns = (session && Array.isArray(session.turns)) ? session.turns : [];
  var last = turns.length > 0 ? turns[turns.length - 1] : null;
  var inputTokens = Number(last && last.tokens_in) || 0;
  var contextLength = Number(session && session.context_length) || 0;
  if (window.TokenMeter) {
    return <window.TokenMeter inputTokens={inputTokens} contextLength={contextLength} onCompact={null} />;
  }
  return (
    <span className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>{inputTokens} tok</span>
  );
}

// ---------------------------------------------------------------------------
// ST_isAutonomous — mirrors primer/session/autonomy.py::session_is_autonomous
// exactly: an explicit `session.autonomous` flag wins; otherwise derive from
// the binding kind (graph ⇒ autonomous, agent ⇒ interactive). Gates the
// agent run view's Stop/End/Restart control set (interactive,
// SessionAgentPanel) vs. the graph run view's Pause/Cancel/Restart control
// set (autonomous, SessionGraphPanel).
// ---------------------------------------------------------------------------

function ST_isAutonomous(session) {
  if (!session) return false;
  if (session.autonomous != null) return !!session.autonomous;
  var kind = (session.binding && session.binding.kind) || session.binding_kind || null;
  return kind === "graph";
}

// ---------------------------------------------------------------------------
// ST_sessionRowToTranscript / ST_coalesceAssistantRows / ST_sessionTranscriptRows
//   — adapt SA_toTranscript's rows (Task 11) to what chat-refactor's
//   <Transcript>/<Message> row renderer (ui/components/chat/transcript.jsx)
//   actually reads off the top level of each row (m.text, m.arguments,
//   m.result, m.id, ...) — mirroring window.chatFlatten's `{...payload,
//   ...row}` spread for a ChatMessage. SA_toTranscript deliberately keeps
//   `payload` NESTED (locked contract, tests/ui/test_session_adapter.py), so
//   this spreads it back out here rather than changing that file.
//
//   A couple of field names differ between a SessionMessageRecord's payload
//   (primer/session/persistence.py) and the ChatMessage wire shape Message()
//   was built against: a tool_result's pairing key is `call_id`
//   (<Transcript> pairs tool_call<->tool_result by `.id`) and its output
//   lives under `output` (Message() reads `.result`) — aliased below.
//   Divider/lifecycle/interaction rows (graph_transition/invocation_divider/
//   yielded/resumed/done/cancelled/error) have no dedicated Message() branch
//   for these collapsed kinds, so they fall through to the generic bubble;
//   seeding `.text` from the divider label (or the nearest payload field)
//   keeps that bubble from rendering blank.
// ---------------------------------------------------------------------------

function ST_sessionRowToTranscript(row) {
  var payload = row.payload || {};
  var flat = Object.assign({}, payload, {
    seq: row.seq,
    kind: row.kind,
    nodeId: row.nodeId,
    created_at: row.createdAt,
  });
  if (row.kind === "tool_result") {
    if (flat.id == null && payload.call_id != null) flat.id = payload.call_id;
    if (flat.result === undefined && payload.output !== undefined) flat.result = payload.output;
  }
  if (flat.text == null && flat.content == null) {
    flat.text = row.label || payload.message || payload.reason || payload.stop_reason || payload.tool_name || "";
  }
  return flat;
}

// Merges a run of consecutive per-token "assistant_message" rows (each
// SA_toTranscript's 1:1 mapping of one ASSISTANT_TOKEN SessionMessageRecord)
// into a single bubble — same idea as window.chatCoalesce
// (ui/components/chat/use-transcript.js), but keyed off `text` (the
// session's own payload field, primer/session/persistence.py) rather than
// `delta` (the chat WS frame's field), and stamping startSeq/endSeq the same
// way — <Transcript>'s row key (`am-${startSeq}-${endSeq}`) needs them, or
// every assistant bubble in a session collides on the same React key.
function ST_coalesceAssistantRows(rows) {
  var out = [];
  var buffer = null;
  function flush() {
    if (buffer && buffer.text.trim().length > 0) out.push(buffer);
    buffer = null;
  }
  for (var i = 0; i < rows.length; i++) {
    var m = rows[i];
    if (m.kind === "assistant_message") {
      var delta = typeof m.text === "string" ? m.text : "";
      if (!buffer) {
        buffer = Object.assign({}, m, { text: delta, startSeq: m.seq, endSeq: m.seq });
      } else {
        buffer.text += delta;
        buffer.endSeq = m.seq;
      }
      continue;
    }
    flush();
    out.push(m);
  }
  flush();
  return out;
}

function ST_sessionTranscriptRows(records, session) {
  var mapped = window.SA_toTranscript(records, session).map(ST_sessionRowToTranscript);
  return ST_coalesceAssistantRows(mapped);
}

// ---------------------------------------------------------------------------
// ST_filterRecordsByNode — narrow the raw session record stream to a single
// node's records (record.node_id === nodeId). A null/empty nodeId means
// "all nodes" (no filter, the full transcript). Used by SessionGraphPanel to
// converge the per-node SD_NodeTurnLog into the shared session <Transcript>
// (fix #9): selecting a node in the run-view filters the transcript instead
// of opening a second per-node panel.
// ---------------------------------------------------------------------------

function ST_filterRecordsByNode(records, nodeId) {
  if (!nodeId) return records || [];
  var out = [];
  var list = records || [];
  for (var i = 0; i < list.length; i++) {
    if (list[i] && list[i].node_id === nodeId) out.push(list[i]);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Optimistic "queued" send rows (fix #8). Steering a RUNNING/WAITING graph
// session queues the message as the next turn server-side; the persisted
// USER_INPUT record only surfaces via the adapter tap once the running turn
// yields. Until then nothing renders and it looks like nothing happened, so
// the panel appends a LOCAL placeholder row immediately on send and drops it
// once the real record lands.
//
// ST_queuedTranscriptRow — shape one local pending send as a user_message
// transcript row, clearly marked "(queued)" and carrying `clientId` so
// <Transcript> keys it off `pending-${clientId}` (no collision with a real
// seq'd row) and `queued`/`pending` flags for any styling.
// ---------------------------------------------------------------------------

function ST_queuedTranscriptRow(pendingSend) {
  return {
    kind: "user_message",
    text: pendingSend.text + " (queued)",
    clientId: pendingSend.id,
    queued: true,
    pending: true,
    nodeId: null,
  };
}

// ST_reconcileQueued — drop any local pending send whose text now appears as
// a persisted USER_INPUT record in the real stream (the running turn yielded
// and the steer landed via the adapter tap). Matches one-to-one on the
// trimmed text so two identical queued sends reconcile against two real
// records, not one. Returns the SAME array reference when nothing reconciles
// so a caller's setState no-ops (no render churn / effect loop).
function ST_reconcileQueued(pendingSends, records) {
  var prev = pendingSends || [];
  if (!prev.length) return prev;
  var realTexts = [];
  var list = records || [];
  for (var i = 0; i < list.length; i++) {
    var m = list[i];
    if (!m || m.kind !== "user_input") continue;
    var pl = m.payload || {};
    var t = pl.text != null ? pl.text : (pl.message != null ? pl.message : (pl.instruction != null ? pl.instruction : ""));
    realTexts.push(String(t).trim());
  }
  if (!realTexts.length) return prev;
  var next = [];
  for (var k = 0; k < prev.length; k++) {
    var idx = realTexts.indexOf(prev[k].text);
    if (idx >= 0) { realTexts.splice(idx, 1); continue; }
    next.push(prev[k]);
  }
  return next.length === prev.length ? prev : next;
}


// ---------------------------------------------------------------------------
// SessionAgentPanel — agent run view = a session-backed <Conversation>.
//   header: title · status pill · turn · token meter + End/Restart
//   body  : <Transcript> fed by SA_toTranscript (Task 11) + <Composer>
//           docked at the bottom — no dedicated steer button (steering IS
//           sending a message, studio-agents-interact §4.2). Composer's own
//           Stop affordance maps to the adapter's stop() (POST .../interrupt);
//           End/Restart are the two extra header controls this panel adds
//           (POST .../cancel and .../restart respectively) since the brief's
//           interactive control set (§Interface) has no Pause.
// ---------------------------------------------------------------------------

function SessionAgentPanel({ wid, sid, session, pushToast }) {
  var StatusPill = window.StatusPill;
  var { useMutation } = window.primerApi;
  var conv = window.SA_useSessionConversation({ sid: sid, wid: wid });
  var [composerText, setComposerText] = React.useState("");
  var scrollRef = React.useRef(null);

  var title = (session && (session.name || session.id)) || sid;
  var turnNo = (session && (session.turn_no != null ? session.turn_no : session.turn_count)) || 0;
  var status = (session && session.status) || conv.status;
  var isEnded = status === "ended";
  var turnInFlight = conv.turnStatus === "claimable" || conv.turnStatus === "running";
  var agentId = (session && session.binding && session.binding.agent_id) || null;

  function toastErr(title) {
    return function (err) {
      if (typeof pushToast !== "function") return;
      pushToast({
        kind: "error",
        title: (err && err.title) || title,
        detail: (err && err.detail) || (err && err.message),
        requestId: err && err.requestId,
      });
    };
  }

  var invalidates = ["studio-session:" + sid, "session-adapter:row:" + sid, "sessions:list"];

  var endMut = useMutation(
    function () { return conv.end(); },
    {
      invalidates: invalidates,
      onSuccess: function () { pushToast && pushToast({ kind: "warning", title: "Session ended" }); },
      onError: toastErr("End failed"),
    }
  );
  var restartMut = useMutation(
    function () { return conv.restart(); },
    {
      invalidates: invalidates,
      onSuccess: function () { pushToast && pushToast({ kind: "success", title: "Session restarted" }); },
      onError: toastErr("Restart failed"),
    }
  );

  function onSend() {
    var text = composerText.trim();
    if (!text) return;
    var p = conv.sendMessage(text);
    setComposerText("");
    if (p && typeof p.catch === "function") p.catch(toastErr("Send failed"));
  }

  function onStop() {
    var p = conv.stop();
    if (p && typeof p.catch === "function") p.catch(toastErr("Stop failed"));
  }

  var rows = React.useMemo(
    function () { return ST_sessionTranscriptRows(conv.messages, session); },
    [conv.messages, session]
  );

  // Stick-to-bottom: a session transcript has no lazy-load-older (history is
  // a single bounded fetch, session-adapter.jsx), so unlike <Conversation>
  // this only ever needs to follow the tail as new rows arrive.
  React.useEffect(function () {
    var el = scrollRef.current;
    if (!el) return undefined;
    var raf = requestAnimationFrame(function () { el.scrollTop = el.scrollHeight; });
    return function () { cancelAnimationFrame(raf); };
  }, [rows.length]);

  return (
    <div
      data-testid="panel-agent"
      style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
    >
      <div
        style={{
          display: "flex", alignItems: "center", gap: 10, padding: "8px 14px",
          borderBottom: "1px solid var(--border)", flex: "0 0 auto", flexWrap: "wrap",
        }}
        data-testid="panel-agent-header"
      >
        <span style={{ color: "var(--accent)", flexShrink: 0 }}>◆</span>
        <span className="mono" style={{ fontSize: 13, fontWeight: 600, color: "var(--text)" }}>{title}</span>
        {StatusPill && session && <StatusPill status={session.status} />}
        <span className="mono" style={{ fontSize: 11, color: "var(--text-4)" }}>turn {turnNo}</span>
        <div style={{ flex: 1 }} />
        <ST_TokenMeterInline session={session} />
        <Btn
          size="sm"
          kind="danger"
          icon="x-circle"
          disabled={!wid || isEnded || endMut.loading}
          onClick={function () { if (wid) endMut.mutate(); }}
          data-testid="ctrl-end"
          title="End this session (cancel — hard stop)"
        >End</Btn>
        {isEnded && (
          <Btn
            size="sm"
            kind="primary"
            icon="refresh"
            disabled={!wid || restartMut.loading}
            onClick={function () { if (wid) restartMut.mutate(); }}
            data-testid="ctrl-restart"
            title="Re-open this ended session and invoke it"
          >Restart</Btn>
        )}
      </div>
      <window.Transcript
        messages={rows}
        chatId={sid}
        agentId={agentId}
        wsState={conv.wsState}
        waitingForReply={false}
        turnStatus={conv.turnStatus}
        pendingToolCall={null}
        sendMessage={conv.sendMessage}
        onRewind={null}
        // No compaction concept for a session transcript — a boundary of
        // +Infinity means "nothing is ever past it", which keeps
        // <Transcript>'s per-message rewind icon (a chat-only affordance,
        // Task F3) from rendering on every user row for a surface that has
        // no POST /rewind endpoint to back it.
        compactionBoundarySeq={Number.MAX_SAFE_INTEGER}
        scrollRef={scrollRef}
        onScroll={function () {}}
        loadingOlder={false}
        hasMoreOlder={false}
      />
      {/* Task 14: this session's own pending interaction, inline — the
          session-scoped counterpart to the global right-sidebar Action
          Required list (studio-activity.jsx). Renders nothing when there's
          nothing parked. */}
      <ST_InlineYields wid={wid} sid={sid} pending={conv.pending} messages={conv.messages} pushToast={pushToast} />
      <div
        style={{
          borderTop: "1px solid var(--border)", padding: 14,
          display: "flex", gap: 8, alignItems: "stretch", flex: "0 0 auto",
        }}
      >
        <window.Composer
          value={composerText}
          onChange={setComposerText}
          onSend={onSend}
          onStop={onStop}
          running={turnInFlight}
          // ENDED no longer disables the composer: sendMessage's
          // steer call reopens an ended session (invocation divider +
          // fresh run) instead of erroring, so typing + sending here IS
          // the restart affordance. A turn in flight still swaps to Stop
          // via `running` above; the explicit Restart button is a
          // separate, no-message re-run.
          disabled={false}
          attachments={[]}
          onAttach={function () {}}
          onRemoveAttachment={function () {}}
          slashCommands={[]}
          mentionSources={[]}
          schemaInvalid={false}
          wsState={conv.wsState}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SessionGraphPanel — graph run view (Task 13).
//   header: name · status · progress (superstep N · X/Y · current) + the
//           toggle-viz control + the AUTONOMOUS control set (pause + cancel,
//           restart once ended — NO Stop/End, those are the INTERACTIVE
//           (agent) set's terms; this panel never calls /interrupt).
//   top   : the reused SD_GraphRunView (graph canvas + node inspector),
//           shown/hidden by the viz toggle (S6 — OFF collapses to chat only).
//   bottom: the SAME session-backed <Transcript>/<Composer> the agent panel
//           uses (SA_useSessionConversation) — graph_transition records
//           SA_toTranscript already maps to divider rows
//           (SA_KIND_TO_TRANSCRIPT), so node/phase transitions render
//           inline with no extra plumbing here.
//   S7: mounting this panel never fires a control — SD_GraphRunView's own
//       effects only GET/poll/tail, SA_useSessionConversation's effects
//       only fetch history + tail the tap, and the three mutations below
//       are wired exclusively to onClick handlers. Opening the panel on an
//       already-running auto_start graph therefore never pauses/steers it.
// ---------------------------------------------------------------------------

function SessionGraphPanel({ wid, sid, gid, rid, session, pushToast }) {
  var StatusPill = window.StatusPill;
  var { useMutation, apiFetch } = window.primerApi;

  // gid: accept an explicit prop (this task's interface) but fall back to
  // deriving it from the session binding exactly like the pre-Task-13
  // panel did — ST_SessionPanel (the only caller today) doesn't pass
  // gid/rid, so this keeps that call site working unchanged.
  gid = gid || (session && session.binding && session.binding.graph_id
    ? session.binding.graph_id
    : (session && session.graph_id) || null);
  // rid: for a graph-bound session the run IS the session, so the run id
  // passed to SD_GraphRunView below is always `sid` (matching its
  // pre-existing rid={sid} call in session-detail.jsx and the
  // test_reuses_sd_graph_run_view pin) — `rid` is accepted here only for
  // interface parity with SD_GraphRunView's own prop name.

  var title = (session && (session.name || session.id)) || sid;
  var superstep = (session && (session.turn_no != null ? session.turn_no : session.turn_count)) || 0;
  var status = session && session.status;
  var isTerminal = !!(session && window.SESSION_TERMINAL && window.SESSION_TERMINAL.has(status));
  var isEnded = status === "ended";

  // Progress summary if the row carries node counts (best-effort; the
  // run-view itself owns the authoritative per-node state).
  var done = session && (session.nodes_done != null ? session.nodes_done : null);
  var total = session && (session.nodes_total != null ? session.nodes_total : null);
  var current = session && (session.current_node || session.active_node) || null;

  // Viz toggle (S6) — a pure render toggle, no network effect either way.
  // Defaults ON so opening the panel shows the live run exactly like the
  // pre-Task-13 panel did.
  var [showViz, setShowViz] = React.useState(true);

  // The SAME session-backed conversation hook the agent panel uses.
  var conv = window.SA_useSessionConversation({ sid: sid, wid: wid });
  var [composerText, setComposerText] = React.useState("");
  var scrollRef = React.useRef(null);

  // fix #9: selected node from the run-view. null = all nodes (full
  // transcript). Driven by SD_GraphRunView's opt-in onNodeSelect below;
  // filters the shared <Transcript> to that node's records.
  var [selectedNode, setSelectedNode] = React.useState(null);

  // fix #8: local optimistic "queued" sends — [{ id, text }] — appended to
  // the transcript the instant the operator steers a busy (non-idle) session,
  // reconciled away once the real USER_INPUT record arrives via the adapter.
  var [pendingSends, setPendingSends] = React.useState([]);

  // Non-idle = a turn is in flight or the session is actively running/waiting,
  // so a steer queues behind it rather than surfacing immediately. Reads both
  // the polled session row and the adapter's live turn_status.
  var turnInFlight = conv.turnStatus === "claimable" || conv.turnStatus === "running";
  var liveStatus = conv.status || status;
  var isBusy = liveStatus === "running" || liveStatus === "waiting" || turnInFlight;

  function toastErr(t) {
    return function (err) {
      if (typeof pushToast !== "function") return;
      pushToast({
        kind: "error",
        title: (err && err.title) || t,
        detail: (err && err.detail) || (err && err.message),
        requestId: err && err.requestId,
      });
    };
  }

  var invalidates = ["studio-session:" + sid, "session-adapter:row:" + sid, "sessions:list"];

  // Autonomous control set (brief §Interface): Pause + Cancel, Restart once
  // ended. Cancel/Restart reuse the session adapter's own end()/restart()
  // — the identical POST .../cancel and .../restart calls the agent
  // panel's End/Restart hit — so the network wiring isn't duplicated.
  // Pause has no adapter equivalent (its interface is Stop/End/Restart
  // only), so it is the one fresh mutation here, against the same
  // workspace-scoped POST .../pause the pre-Task-13 graph cluster
  // used (reused endpoint, not a new one).
  var pauseMut = useMutation(
    function () { return apiFetch("POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid) + "/pause"); },
    { invalidates: invalidates, onSuccess: function () { pushToast && pushToast({ kind: "success", title: "Session paused" }); }, onError: toastErr("Pause failed") }
  );
  var cancelMut = useMutation(
    function () { return conv.end(); },
    { invalidates: invalidates, onSuccess: function () { pushToast && pushToast({ kind: "warning", title: "Cancel signal sent" }); }, onError: toastErr("Cancel failed") }
  );
  var restartMut = useMutation(
    function () { return conv.restart(); },
    { invalidates: invalidates, onSuccess: function () { pushToast && pushToast({ kind: "success", title: "Session restarted" }); }, onError: toastErr("Restart failed") }
  );

  // fix #9: when a node is selected, filter the record stream to that node's
  // records (node_id === selectedNode) BEFORE mapping — coalescing then runs
  // scoped to the node. fix #8: append the local optimistic queued rows after
  // the real rows so a just-steered message shows immediately.
  var rows = React.useMemo(
    function () {
      var base = ST_sessionTranscriptRows(ST_filterRecordsByNode(conv.messages, selectedNode), session);
      if (!pendingSends.length) return base;
      return base.concat(pendingSends.map(ST_queuedTranscriptRow));
    },
    [conv.messages, session, selectedNode, pendingSends]
  );

  // fix #8: reconcile — drop any optimistic queued send once its persisted
  // USER_INPUT record lands in the real stream (via the adapter tap).
  React.useEffect(function () {
    setPendingSends(function (prev) { return ST_reconcileQueued(prev, conv.messages); });
  }, [conv.messages]);

  // Stick-to-bottom — same rationale as the agent panel.
  React.useEffect(function () {
    var el = scrollRef.current;
    if (!el) return undefined;
    var raf = requestAnimationFrame(function () { el.scrollTop = el.scrollHeight; });
    return function () { cancelAnimationFrame(raf); };
  }, [rows.length]);

  function onSend() {
    var text = composerText.trim();
    if (!text) return;
    var p = conv.sendMessage(text);
    setComposerText("");
    // fix #8: on a non-idle session the steer queues behind the running turn,
    // so show a local "(queued)" placeholder immediately. Reconciled by the
    // effect above when the real record arrives; removed here if the send
    // itself fails so a rejected steer doesn't leave a stuck placeholder.
    if (isBusy) {
      var localId = "queued-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
      setPendingSends(function (prev) { return prev.concat([{ id: localId, text: text }]); });
      if (p && typeof p.catch === "function") {
        p.catch(function (err) {
          setPendingSends(function (prev) { return prev.filter(function (x) { return x.id !== localId; }); });
          toastErr("Send failed")(err);
        });
      }
    } else if (p && typeof p.catch === "function") {
      p.catch(toastErr("Send failed"));
    }
  }

  return (
    <div
      data-testid="panel-graph"
      style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
    >
      <div
        style={{
          display: "flex", alignItems: "center", gap: 10, padding: "8px 14px",
          borderBottom: "1px solid var(--border)", flex: "0 0 auto", flexWrap: "wrap",
        }}
        data-testid="panel-graph-header"
      >
        <span style={{ color: "var(--violet)", flexShrink: 0 }}>◈</span>
        <span className="mono" style={{ fontSize: 13, fontWeight: 600, color: "var(--text)" }}>{title}</span>
        {StatusPill && session && <StatusPill status={session.status} />}
        <span className="mono" style={{ fontSize: 11, color: "var(--text-4)" }}>
          superstep {superstep}
          {(done != null && total != null) ? " · " + done + "/" + total : ""}
          {current ? " · " + current : ""}
        </span>
        <div style={{ flex: 1 }} />
        <Btn
          size="sm"
          kind={showViz ? "primary" : "ghost"}
          icon="graph"
          onClick={function () { setShowViz(function (v) { return !v; }); }}
          data-testid="ctrl-toggle-viz"
          title={showViz ? "Hide the run-view canvas — chat only" : "Show the run-view canvas"}
        >{showViz ? "Hide viz" : "Show viz"}</Btn>
        <Btn
          size="sm"
          icon="pause"
          disabled={!wid || status !== "running" || pauseMut.loading}
          onClick={function () { if (wid) pauseMut.mutate(); }}
          data-testid="ctrl-pause"
          title={status !== "running" ? "Enabled only when running" : "Pause after current turn"}
        >Pause</Btn>
        <Btn
          size="sm"
          kind="danger"
          icon="stop"
          disabled={!wid || isTerminal || cancelMut.loading}
          onClick={function () { if (wid) cancelMut.mutate(); }}
          data-testid="ctrl-cancel"
          title="Cancel the run"
        >Cancel</Btn>
        {isEnded && (
          <Btn
            size="sm"
            kind="primary"
            icon="refresh"
            disabled={!wid || restartMut.loading}
            onClick={function () { if (wid) restartMut.mutate(); }}
            data-testid="ctrl-restart"
            title="Re-open this ended run and invoke it"
          >Restart</Btn>
        )}
      </div>

      {showViz && (
        <div
          data-testid="graph-viz-region"
          style={{ flex: "0 0 auto", height: 360, minHeight: 0, overflow: "auto", borderBottom: "1px solid var(--border)" }}
        >
          {wid && gid && window.SD_GraphRunView
            ? <window.SD_GraphRunView
                gid={gid}
                rid={sid}
                wid={wid}
                session={session}
                pushToast={pushToast}
                // fix #9: opt into convergence — selecting a node filters the
                // shared transcript (below) instead of opening a per-node
                // panel. hideInspector drops the 360px node-event-stream
                // inspector entirely so the graph canvas fills the run view;
                // its content is redundant with the converged transcript below.
                onNodeSelect={setSelectedNode}
                hideInspector={true}
              />
            : (
              <div className="muted text-sm" style={{ padding: 20, textAlign: "center", color: "var(--text-4)" }}>
                Run view unavailable — graph binding or workspace missing.
              </div>
            )}
        </div>
      )}

      {/* fix #9: active node filter banner — shown only when a node is
          selected. Names the node the transcript is scoped to and offers a
          one-click return to the full (all-nodes) transcript. */}
      {selectedNode && (
        <div
          data-testid="graph-node-filter"
          style={{
            display: "flex", alignItems: "center", gap: 8, padding: "6px 14px",
            borderBottom: "1px solid var(--border)", background: "var(--bg-2)",
            flex: "0 0 auto", fontSize: 11.5, color: "var(--text-3)",
          }}
        >
          <span style={{ color: "var(--violet)", flexShrink: 0 }}>◈</span>
          <span>
            Showing <span className="mono" style={{ color: "var(--text)", fontWeight: 600 }}>{selectedNode}</span> only
          </span>
          <div style={{ flex: 1 }} />
          <Btn
            size="sm"
            kind="ghost"
            icon="x"
            data-testid="graph-node-filter-clear"
            title="Show the full transcript (all nodes)"
            onClick={function () { setSelectedNode(null); }}
          >All nodes</Btn>
        </div>
      )}
      <window.Transcript
        messages={rows}
        chatId={sid}
        agentId={null}
        wsState={conv.wsState}
        waitingForReply={false}
        turnStatus={conv.turnStatus}
        pendingToolCall={null}
        sendMessage={conv.sendMessage}
        onRewind={null}
        // Same rationale as the agent panel: no compaction concept for a
        // session transcript, and no per-message rewind endpoint here either.
        compactionBoundarySeq={Number.MAX_SAFE_INTEGER}
        scrollRef={scrollRef}
        onScroll={function () {}}
        loadingOlder={false}
        hasMoreOlder={false}
      />
      {/* Task 14: same inline pending-interaction affordance as the agent
          panel — a graph-bound session parks on a yield exactly the same
          way (studio-agents-interact §5.4), so it needs the same inline
          Approve/Deny/respond surface over its own chat stream. */}
      <ST_InlineYields wid={wid} sid={sid} pending={conv.pending} messages={conv.messages} pushToast={pushToast} />
      <div
        style={{
          borderTop: "1px solid var(--border)", padding: 14,
          display: "flex", gap: 8, alignItems: "stretch", flex: "0 0 auto",
        }}
      >
        <window.Composer
          value={composerText}
          onChange={setComposerText}
          onSend={onSend}
          // NO Stop affordance on the autonomous set (brief §Interface) —
          // running is always false so <Composer> never swaps to its Stop
          // control (which is reserved for the agent panel's interactive
          // hard-preempt call; this panel must never trigger it). Steering
          // IS sending a message here too, same as the agent panel.
          onStop={function () {}}
          running={false}
          // Same rationale as the agent panel's Composer above: ENDED no
          // longer disables sending — steering an ended session's
          // sendMessage restarts it (reopen + invocation divider + run).
          disabled={false}
          attachments={[]}
          onAttach={function () {}}
          onRemoveAttachment={function () {}}
          slashCommands={[]}
          mentionSources={[]}
          schemaInvalid={false}
          wsState={conv.wsState}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ST_InlineYields — Task 14: the ACTIVE session's own pending interaction(s)
// rendered INLINE in its stream (right after <Transcript>, before the
// Composer) — the session-scoped counterpart to studio-activity.jsx's
// GLOBAL "Action Required" list. Both surfaces hit the exact same
// endpoints (tool_approval/respond, ask_user/respond, yields/{id}/cancel)
// so responding here clears the identical server-side yield the global
// sidebar is tracking — there is no separate "inline" state to drift.
//
// Data: `pending` is session-adapter.jsx's own `conv.pending`
// (GET .../workspaces/{wid}/sessions/{sid}/yields/pending, Task 10, polled
// every 4s) — passed down from the panel, no second fetch here.
//
// Reconcile: rather than opening a SECOND EventSource onto the session-
// scoped tap (the adapter already tails it into `conv.messages`), this
// watches the tail of `messages` for a fresh "yielded"/"resumed" record and
// force-refetches both the session-scoped AND workspace-wide pending
// caches via window.primerApi._resource — the same findKeys/refetchKey
// primitive useMutation's own `invalidates` list uses (use-mutation.js).
// One live connection (the adapter's), two caches kept in sync.
//
// Placed AFTER SessionGraphPanel (below) rather than up here next to its
// sibling pure helpers (ST_isAutonomous / ST_sessionTranscriptRows) — those
// are deliberately JSX-free so tests/ui/test_studio_run_view_interactive.py
// + test_studio_graph_run_view.py can `py_mini_racer`-eval that exact slice
// of the file; this component (JSX-bearing) would break that eval if it
// sat inside the same gap.
// ---------------------------------------------------------------------------

function ST_yieldInvalidates(wid, sid) {
  return ["session-adapter:pending:" + sid, "studio-yields-pending:" + wid];
}

function ST_InlineYields({ wid, sid, pending, messages, pushToast }) {
  var apiFetch = window.primerApi.apiFetch;
  var useMutation = window.primerApi.useMutation;
  var invalidates = ST_yieldInvalidates(wid, sid);

  function toastErr(title) {
    return function (err) {
      if (typeof pushToast !== "function") return;
      pushToast({
        kind: "error",
        title: (err && err.title) || title,
        detail: (err && err.detail) || (err && err.message),
        requestId: err && err.requestId,
      });
    };
  }

  var approveMut = useMutation(
    function (item) {
      return apiFetch(
        "POST",
        "/sessions/" + encodeURIComponent(item.session_id) + "/tool_approval/respond",
        { tool_call_id: item.tool_call_id, decision: "approved" }
      );
    },
    { invalidates: invalidates, onError: toastErr("Approve failed") }
  );
  var rejectMut = useMutation(
    function (item) {
      return apiFetch(
        "POST",
        "/sessions/" + encodeURIComponent(item.session_id) + "/tool_approval/respond",
        { tool_call_id: item.tool_call_id, decision: "rejected", reason: "" }
      );
    },
    { invalidates: invalidates, onError: toastErr("Reject failed") }
  );
  var respondMut = useMutation(
    function (payload) {
      return apiFetch(
        "POST",
        "/sessions/" + encodeURIComponent(payload.item.session_id) + "/ask_user/respond",
        { tool_call_id: payload.item.tool_call_id, response: payload.text }
      );
    },
    { invalidates: invalidates, onError: toastErr("Respond failed") }
  );
  var cancelMut = useMutation(
    function (item) {
      return apiFetch(
        "POST",
        "/sessions/" + encodeURIComponent(item.session_id) + "/yields/" + encodeURIComponent(item.tool_call_id) + "/cancel",
        { reason: "operator cancelled" }
      );
    },
    { invalidates: invalidates, onError: toastErr("Cancel failed") }
  );

  // Tap-tail reconcile — see file-header comment above. Only reacts to a
  // NEW last record (guarded by seq) so this doesn't refetch on every
  // unrelated render (assistant tokens streaming in, etc).
  var lastSeqRef = React.useRef(null);
  React.useEffect(function () {
    if (!messages || !messages.length) return;
    var last = messages[messages.length - 1];
    if (last.seq === lastSeqRef.current) return;
    lastSeqRef.current = last.seq;
    if (last.kind !== "yielded" && last.kind !== "resumed") return;
    var resourceApi = window.primerApi._resource;
    if (!resourceApi) return;
    invalidates.forEach(function (baseKey) {
      resourceApi.findKeys(baseKey).forEach(function (key) { resourceApi.refetchKey(key); });
    });
  }, [messages]); // eslint-disable-line react-hooks/exhaustive-deps

  var [drafts, setDrafts] = React.useState({});
  function getDraft(id) { return drafts[id] || ""; }
  function setDraft(id, text) {
    setDrafts(function (prev) {
      var next = Object.assign({}, prev);
      next[id] = text;
      return next;
    });
  }

  if (!wid || !sid || !pending || !pending.length) return null;

  var busy = approveMut.loading || rejectMut.loading || respondMut.loading || cancelMut.loading;

  return (
    <div
      data-testid="session-inline-yields"
      style={{ borderTop: "1px solid var(--border)", background: "var(--bg-2)", flex: "0 0 auto" }}
    >
      {pending.map(function (item, idx) {
        var isApproval = item.kind === "approval";
        var isAsk = item.kind === "ask_user";
        var isCancelable = item.kind === "watch_files" || item.kind === "sleep";
        var actionable = !!item.tool_call_id;
        var draft = getDraft(item.tool_call_id || String(idx));

        return (
          <div
            key={item.tool_call_id || idx}
            data-testid="session-yield-item"
            style={{ padding: "10px 14px", display: "flex", flexDirection: "column", gap: 8 }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ color: "var(--amber)", fontSize: 12 }}>⚠</span>
              <span
                style={{
                  fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.05em",
                  fontWeight: 600, color: "var(--amber)",
                }}
              >
                Waiting on you — {item.kind}
              </span>
            </div>

            {item.prompt && (
              <div style={{ fontSize: 12.5, lineHeight: 1.5, color: "var(--text-2)", whiteSpace: "pre-wrap" }}>
                {item.prompt}
              </div>
            )}

            {isApproval && (
              <div style={{ display: "flex", gap: 8 }}>
                <Btn
                  size="sm" kind="primary" icon="check"
                  disabled={!actionable || busy}
                  data-testid="session-yield-approve"
                  onClick={function () { approveMut.mutate(item); }}
                >Approve</Btn>
                <Btn
                  size="sm" kind="danger" icon="x"
                  disabled={!actionable || busy}
                  data-testid="session-yield-deny"
                  onClick={function () { rejectMut.mutate(item); }}
                >Deny</Btn>
              </div>
            )}

            {isAsk && (
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  type="text"
                  data-testid="session-yield-respond"
                  placeholder="Type a response… Enter to send"
                  value={draft}
                  disabled={busy || !actionable}
                  onChange={function (e) { setDraft(item.tool_call_id || String(idx), e.target.value); }}
                  onKeyDown={function (e) {
                    if (e.key !== "Enter" || e.shiftKey) return;
                    e.preventDefault();
                    var text = draft.trim();
                    if (!text) return;
                    respondMut.mutate({ item: item, text: text });
                    setDraft(item.tool_call_id || String(idx), "");
                  }}
                  style={{
                    flex: 1, background: "var(--bg-0)", border: "1px solid var(--border)",
                    borderRadius: 6, padding: "5px 10px", fontSize: 12.5, color: "var(--text)",
                    fontFamily: "inherit", outline: "none",
                  }}
                />
              </div>
            )}

            {isCancelable && (
              <div>
                <Btn
                  size="sm" kind="ghost" icon="x-circle"
                  disabled={!actionable || busy}
                  data-testid="session-yield-cancel"
                  onClick={function () { cancelMut.mutate(item); }}
                >Cancel</Btn>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}


// ---------------------------------------------------------------------------
// ST_SessionPanel — resolves a session tab to the agent or graph panel.
//   Fetches GET /v1/sessions/{ref} (useResource) to read binding.kind.
// ---------------------------------------------------------------------------

function ST_SessionPanel({ wid, sid, pushToast }) {
  var { useResource, apiFetch } = window.primerApi;

  var detail = useResource(
    "studio-session:" + sid,
    function (signal) { return apiFetch("GET", "/sessions/" + encodeURIComponent(sid), null, { signal }); },
    {
      pollMs: 2000,
      pauseWhile: function () {
        // Once terminal, stop polling — nothing left to refresh at this level.
        var st = detail && detail.data && detail.data.status;
        return !!(window.SESSION_TERMINAL && window.SESSION_TERMINAL.has(st));
      },
      deps: [sid],
    }
  );

  var session = detail.data;

  if (detail.loading && !session) {
    return (
      <div className="muted text-sm" style={{ padding: 40, textAlign: "center", color: "var(--text-4)" }}>
        Loading session {sid}…
      </div>
    );
  }
  if (detail.error && !session) {
    return (
      <div style={{ padding: 16 }}>
        <Banner
          kind="error"
          title={(detail.error && detail.error.title) || "Couldn't load session"}
          detail={(detail.error && (detail.error.detail || detail.error.message)) || sid}
          actions={<Btn size="sm" icon="refresh" onClick={detail.refetch}>Retry</Btn>}
        />
      </div>
    );
  }
  if (!session) return null;

  // Route through ST_isAutonomous (the byte-mirror of backend
  // session_is_autonomous): an explicit `session.autonomous` flag wins,
  // else derive from the binding kind (graph ⇒ autonomous). Branching on
  // the raw binding.kind instead would send an explicit-override session
  // (autonomous flag contradicting binding.kind) to the wrong panel. For a
  // session WITHOUT an explicit override this is identical to the old
  // `binding.kind === "graph"` branch (agent/missing ⇒ agent panel).
  // The session's own workspace_id is authoritative; fall back to the route wid.
  var effWid = session.workspace_id || wid;

  if (ST_isAutonomous(session)) {
    return <SessionGraphPanel wid={effWid} sid={sid} session={session} pushToast={pushToast} />;
  }
  return <SessionAgentPanel wid={effWid} sid={sid} session={session} pushToast={pushToast} />;
}

// ---------------------------------------------------------------------------
// ST_FilePreview — render the held content per file class.
//   markdown → renderMarkdown; code → highlighted line-numbered <pre>;
//   image → <img> via files/download; else plain text.
// ---------------------------------------------------------------------------

function ST_FilePreview({ wid, path, content }) {
  var cls = ST_fileClassOf(path);

  if (cls === "image") {
    var src = "/v1/workspaces/" + encodeURIComponent(wid) + "/files/download?path=" + encodeURIComponent(path);
    return (
      <div style={{ padding: 18, display: "grid", placeItems: "center" }} data-testid="file-preview-image">
        <img src={src} alt={ST_basename(path)} style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }} />
      </div>
    );
  }

  if (cls === "markdown" && typeof window.renderMarkdown === "function") {
    return (
      <div
        className="md-body"
        data-testid="file-preview-markdown"
        style={{ maxWidth: 760, padding: "18px 22px", fontSize: 13.5, lineHeight: 1.65 }}
      >
        {window.renderMarkdown(content || "")}
      </div>
    );
  }

  if (cls === "code" && window.primerVendor && window.primerVendor.highlightPython) {
    var lang = ST_CODE_LANGS[ST_extOf(path)] || "";
    var htmlLines = window.primerVendor.highlightPython(content || "", lang);
    return (
      <div
        data-testid="file-preview-code"
        style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 12.5, lineHeight: 1.7, padding: "12px 14px" }}
      >
        {htmlLines.map(function (html, i) {
          return (
            <div key={i} style={{ display: "flex" }}>
              <span style={{ color: "var(--text-4)", width: 34, textAlign: "right", marginRight: 14, userSelect: "none", flexShrink: 0 }}>{i + 1}</span>
              <span style={{ whiteSpace: "pre-wrap", flex: 1 }} dangerouslySetInnerHTML={{ __html: html }} />
            </div>
          );
        })}
      </div>
    );
  }

  // Plain text fallback.
  return (
    <pre
      data-testid="file-preview-text"
      style={{
        fontFamily: "IBM Plex Mono, monospace", fontSize: 12.5, lineHeight: 1.7,
        padding: "12px 14px", margin: 0, whiteSpace: "pre-wrap", color: "var(--text-2)",
      }}
    >{content || ""}</pre>
  );
}

// ---------------------------------------------------------------------------
// FilePanel — preview/edit a workspace file with optimistic-concurrency save.
//   header: breadcrumb path + Preview/Edit toggle + Save (enabled when dirty)
//   body  : preview (markdown/code/image/text) OR a <textarea> editor
//   save  : PUT .../files?path=<ref>&etag=<held>; on 412 → conflict banner
//           (Reload re-GETs + clears dirty; Overwrite re-PUTs without etag).
//   binary/large(>~1MB): preview/download only (no edit toggle).
// ---------------------------------------------------------------------------

function FilePanel({ wid, tab, studio, pushToast }) {
  var { useResource, apiFetch } = window.primerApi;
  var path = tab.ref;
  var tabId = tab.id;
  var mode = (studio.state.fileModes && studio.state.fileModes[tabId]) || "preview";

  // The held read: content + etag we opened the file at. The etag we PUT with
  // is held in a ref so editing doesn't refetch and a successful save can
  // refresh it without re-reading the file.
  var fileRes = useResource(
    "studio-file:" + wid + ":" + path,
    function (signal) {
      return apiFetch(
        "GET",
        "/workspaces/" + encodeURIComponent(wid) + "/files/read?path=" + encodeURIComponent(path) + "&encoding=text",
        null,
        { signal }
      );
    },
    { pollMs: 0, deps: [wid, path] }
  );

  var heldEtagRef = React.useRef(null);
  var [draft, setDraft] = React.useState(null);
  var [conflict, setConflict] = React.useState(false);
  var [saving, setSaving] = React.useState(false);

  var data = fileRes.data || null;
  var content = data && typeof data.content === "string" ? data.content : "";
  var sizeBytes = data && typeof data.size_bytes === "number" ? data.size_bytes : (content ? content.length : 0);
  var isImage = ST_fileClassOf(path) === "image";
  var encoding = data && data.encoding;
  var isBinary = encoding && encoding !== "text";
  var tooLarge = sizeBytes > ST_EDIT_MAX_BYTES;
  var editable = !isImage && !isBinary && !tooLarge;

  // Seed the editor draft + held etag whenever a fresh read lands. Editing
  // mutates `draft` locally; the held etag gates the next save.
  React.useEffect(function () {
    if (data && data.etag !== undefined) {
      heldEtagRef.current = data.etag;
    }
    if (data && typeof data.content === "string") {
      setDraft(data.content);
    }
  }, [data]);

  // A dirty tab is one whose draft diverges from the held content. We mirror
  // that into studio.state.openTabs[*].dirty via the patch() escape hatch so
  // the tab bar + sidebar dirty dots reflect it.
  function setTabDirty(dirty) {
    var openTabs = (studio.state.openTabs || []).map(function (t) {
      if (t.id !== tabId) return t;
      if (!!t.dirty === !!dirty) return t;
      return Object.assign({}, t, { dirty: !!dirty });
    });
    // Only patch when something actually changed (avoid render churn).
    var changed = false;
    for (var i = 0; i < openTabs.length; i++) {
      if (openTabs[i] !== (studio.state.openTabs || [])[i]) { changed = true; break; }
    }
    if (changed) studio.patch({ openTabs: openTabs });
  }

  function onEditInput(e) {
    var next = e.target.value;
    setDraft(next);
    setTabDirty(next !== content);
  }

  function setMode(m) {
    // B1's useStudioState exposes only patch(); the per-tab file mode lives in
    // state.fileModes keyed by tab id.
    var nextModes = Object.assign({}, studio.state.fileModes);
    nextModes[tabId] = m;
    studio.patch({ fileModes: nextModes });
  }

  function toastErr(title, err) {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: (err && err.title) || title,
      detail: (err && (err.detail || err.message)),
      requestId: err && err.requestId,
    });
  }

  // PUT the draft. When `withEtag` is true we send the held etag so the server
  // 412s on a stale write (someone else wrote the file since we opened it);
  // when false we force-overwrite (the Overwrite conflict action).
  async function doSave(withEtag) {
    if (draft == null) return;
    setSaving(true);
    var url = "/workspaces/" + encodeURIComponent(wid) + "/files?path=" + encodeURIComponent(path);
    if (withEtag && heldEtagRef.current) {
      url += "&etag=" + encodeURIComponent(heldEtagRef.current);
    }
    try {
      await apiFetch("PUT", url, { content: draft, encoding: "text" });
      // Success: clear dirty + conflict, refresh the held etag from a re-read.
      setConflict(false);
      setTabDirty(false);
      setSaving(false);
      pushToast && pushToast({ kind: "success", title: "File saved", detail: path });
      // Trigger a re-read to refresh the held etag. refetch() returns undefined
      // here (useResource), so adoption happens in the [data] effect above,
      // which re-seeds heldEtagRef + draft once the new read commits.
      await fileRes.refetch();
    } catch (err) {
      setSaving(false);
      if (err && err.status === 412) {
        // Stale write — surface the conflict banner (Reload / Overwrite).
        setConflict(true);
        return;
      }
      toastErr("Save failed", err);
    }
  }

  function onSave() { doSave(true); }

  // Reload: discard local edits, re-GET (replaces content), clear dirty.
  async function reloadConflict() {
    setConflict(false);
    setTabDirty(false);
    // Trigger a re-read. refetch() returns undefined (useResource), so the
    // [data] effect above performs the adoption: it replaces `draft` with the
    // fresh content and re-seeds heldEtagRef once the new read commits.
    await fileRes.refetch();
  }

  // Overwrite: re-PUT WITHOUT the etag (force), keeping local edits.
  function overwriteConflict() {
    setConflict(false);
    doSave(false);
  }

  var dirty = (draft != null) && (draft !== content);
  var effMode = editable ? mode : "preview";

  // ----- header -----
  var header = (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 8, padding: "8px 14px",
        borderBottom: "1px solid var(--border)", flex: "0 0 auto",
        fontFamily: "IBM Plex Mono, monospace", fontSize: 12, color: "var(--text-3)",
      }}
      data-testid="panel-file-header"
    >
      <span style={{ color: "var(--text-2)", display: "grid", placeItems: "center", flexShrink: 0 }}>
        <Icon name={isImage ? "image" : ST_fileClassOf(path) === "code" ? "code" : "doc"} size={13} />
      </span>
      <span data-testid="file-breadcrumb" style={{ color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{path}</span>
      {sizeBytes ? <span style={{ flexShrink: 0 }}>· {ST_fmtBytes(sizeBytes)}</span> : null}
      <div style={{ flex: 1 }} />

      {editable && (
        <div style={{ display: "flex", border: "1px solid var(--border)", borderRadius: 7, overflow: "hidden", flexShrink: 0 }}>
          <button
            data-testid="file-mode-preview"
            onClick={function () { setMode("preview"); }}
            style={{
              padding: "3px 9px", fontSize: 11.5, border: "none", cursor: "pointer",
              background: effMode === "preview" ? "var(--accent-dim)" : "var(--bg-2)",
              color: effMode === "preview" ? "var(--accent)" : "var(--text-3)",
            }}
          >◉ Preview</button>
          <button
            data-testid="file-mode-edit"
            onClick={function () { setMode("edit"); }}
            style={{
              padding: "3px 9px", fontSize: 11.5, border: "none", cursor: "pointer",
              borderLeft: "1px solid var(--border)",
              background: effMode === "edit" ? "var(--accent-dim)" : "var(--bg-2)",
              color: effMode === "edit" ? "var(--accent)" : "var(--text-3)",
            }}
          >✎ Edit</button>
        </div>
      )}

      {editable && (
        <button
          data-testid="file-save"
          onClick={onSave}
          disabled={!dirty || saving}
          title={dirty ? "Save (PUT with etag)" : "No unsaved changes"}
          style={{
            padding: "3px 10px", fontSize: 11.5, fontWeight: 600, borderRadius: 6,
            border: "1px solid oklch(0.82 0.18 145 / 0.4)", cursor: dirty && !saving ? "pointer" : "default",
            background: "var(--green-dim)", color: "var(--green)",
            opacity: dirty && !saving ? 1 : 0.45, flexShrink: 0,
          }}
        >⤓ {saving ? "Saving…" : "Save"}</button>
      )}

      {!editable && (
        <a
          data-testid="file-download"
          href={"/v1/workspaces/" + encodeURIComponent(wid) + "/files/download?path=" + encodeURIComponent(path)}
          target="_blank"
          rel="noreferrer noopener"
          style={{ padding: "3px 10px", fontSize: 11.5, color: "var(--text-2)", textDecoration: "none", border: "1px solid var(--border)", borderRadius: 6, flexShrink: 0 }}
        >↓ Download</a>
      )}
    </div>
  );

  // ----- body -----
  var body;
  if (fileRes.loading && !data) {
    body = <div className="muted text-sm" style={{ padding: 24, textAlign: "center", color: "var(--text-4)" }}>Loading {path}…</div>;
  } else if (fileRes.error && !data) {
    body = (
      <div style={{ padding: 16 }}>
        <Banner
          kind="error"
          title={(fileRes.error && fileRes.error.title) || "Couldn't read file"}
          detail={(fileRes.error && (fileRes.error.detail || fileRes.error.message)) || path}
          actions={<Btn size="sm" icon="refresh" onClick={fileRes.refetch}>Retry</Btn>}
        />
      </div>
    );
  } else if (effMode === "edit") {
    body = (
      <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
        {conflict && (
          <div
            data-testid="file-conflict-banner"
            style={{
              margin: "12px 14px 0", padding: "8px 12px", borderRadius: 7,
              border: "1px solid oklch(0.82 0.16 75 / 0.4)", background: "var(--amber-dim)",
              color: "var(--amber)", fontSize: 12, display: "flex", alignItems: "center", gap: 10,
            }}
          >
            <span style={{ flex: 1 }}>⚠ This file changed on disk since you opened it.</span>
            <b data-testid="file-conflict-reload" onClick={reloadConflict} style={{ cursor: "pointer", textDecoration: "underline" }}>Reload</b>
            <b data-testid="file-conflict-overwrite" onClick={overwriteConflict} style={{ cursor: "pointer", textDecoration: "underline" }}>Overwrite</b>
          </div>
        )}
        <textarea
          data-testid="file-editor"
          value={draft == null ? "" : draft}
          onChange={onEditInput}
          spellCheck={false}
          style={{
            flex: 1, minHeight: 320, width: "100%", background: "var(--bg)", border: 0,
            color: "var(--text)", fontFamily: "IBM Plex Mono, monospace", fontSize: 12.5,
            lineHeight: 1.7, padding: "14px 16px", resize: "none", tabSize: 2, outline: "none",
          }}
        />
      </div>
    );
  } else {
    body = <ST_FilePreview wid={wid} path={path} content={content} />;
  }

  return (
    <div
      data-testid="panel-file"
      style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
    >
      {header}
      <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        {body}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// StudioCenter — center region: CenterTabs + the active panel.
// Props: { wid, studio }  (studio = useStudioState(wid) bag)
// ---------------------------------------------------------------------------

function StudioCenter({ wid, studio }) {
  var s = studio.state;
  var openTabs = s.openTabs || [];
  var activeTabId = s.activeTabId;
  // pushToast threads down from app.jsx → Studio when wired; B1 renders Studio
  // without it today, so every toast call below is guarded and simply no-ops
  // until a later task passes it. The 412-conflict UX uses an inline banner
  // (not a toast), so the critical flow does not depend on this.
  var pushToast = studio.pushToast || (window.primerApi && window.primerApi.toastPush) || null;

  var activeTab = null;
  for (var i = 0; i < openTabs.length; i++) {
    if (openTabs[i].id === activeTabId) { activeTab = openTabs[i]; break; }
  }

  var panel;
  if (!activeTab) {
    panel = (
      <div
        className="st-placeholder"
        data-testid="center-empty"
        style={{ flex: 1 }}
      >
        <div>
          <div className="st-ph-kind">Studio</div>
          <div style={{ marginTop: 6 }}>Open a session or file from the sidebar to begin.</div>
        </div>
      </div>
    );
  } else if (activeTab.kind === "session") {
    // key by ref so switching session tabs remounts the resolver cleanly.
    panel = <ST_SessionPanel key={"sess:" + activeTab.ref} wid={wid} sid={activeTab.ref} pushToast={pushToast} />;
  } else if (activeTab.kind === "file") {
    panel = <FilePanel key={"file:" + activeTab.ref} wid={wid} tab={activeTab} studio={studio} pushToast={pushToast} />;
  } else {
    panel = (
      <div className="st-placeholder" data-testid="center-unknown" style={{ flex: 1 }}>
        <div>Unknown tab kind: {String(activeTab.kind)}</div>
      </div>
    );
  }

  return (
    <div
      data-testid="studio-center-inner"
      style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, overflow: "hidden" }}
    >
      <CenterTabs
        openTabs={openTabs}
        activeTabId={activeTabId}
        onFocus={studio.focusTab}
        onClose={studio.closeTab}
        onCloseAll={studio.closeAllTabs}
      />
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {panel}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// No-build exports
// ---------------------------------------------------------------------------
window.StudioCenter = StudioCenter;
window.CenterTabs = CenterTabs;
window.ST_isAutonomous = ST_isAutonomous;
window.ST_sessionTranscriptRows = ST_sessionTranscriptRows;
window.SessionAgentPanel = SessionAgentPanel;
window.SessionGraphPanel = SessionGraphPanel;
window.FilePanel = FilePanel;
