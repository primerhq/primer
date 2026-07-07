/* global React, Icon, Btn, Banner, ApprovalBanner, relativeTime, fmtDate */


function _sdAgeSec(iso) {
  if (!iso) return null;
  if (iso instanceof Date) return (Date.now() - iso.getTime()) / 1000;
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

function _sdToastErr(pushToast, fallbackTitle) {
  return (err) => {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: err?.title || fallbackTitle,
      detail: err?.detail || err?.message,
      requestId: err?.requestId,
    });
  };
}

// Encode a single-session tap resume cursor that the server's TapCursor.decode
// accepts: base64url (no padding) of {"known_as_of": iso, "seqs": {sid: seq}}
// with sorted keys + compact separators (matches primer/tap/cursor.py:encode).
// known_as_of only gates *new-session* discovery; for a single-session resume
// the epoch is correct (we already know the one session we are tailing).
function _slsEncodeCursor(sid, seq) {
  const payload = { known_as_of: "1970-01-01T00:00:00+00:00", seqs: { [sid]: seq } };
  const json = JSON.stringify(payload);
  // btoa over UTF-8 bytes, then make it URL-safe and strip padding.
  let b64;
  try {
    b64 = btoa(unescape(encodeURIComponent(json)));
  } catch (_e) {
    return null;
  }
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function TurnRow({ turn, index }) {
  const [open, setOpen] = React.useState(turn.status === "running" || turn.status === "failed");
  return (
    <div className={`turn ${turn.status || ""}`}>
      <div className="turn-dot">{turn.status === "running" ? <Icon name="zap" size={11} /> : index + 1}</div>
      <div className="turn-body">
        <div className="turn-h" onClick={() => setOpen(!open)} style={{ cursor: "pointer" }}>
          <Icon name={open ? "chevron-down" : "chevron-right"} size={11} className="muted" />
          <span>Turn {index + 1}</span>
          {turn.started_at && <span className="time">· {fmtDate(new Date(turn.started_at)).slice(11)}</span>}
          {turn.duration_ms != null && <span className="dur">· {(turn.duration_ms / 1000).toFixed(1)}s</span>}
          {turn.status === "running" && <span className="pill pill-running" style={{ marginLeft: 4 }}><span className="dot"></span>running</span>}
          {turn.status === "failed" && <span className="pill pill-failed" style={{ marginLeft: 4 }}><span className="dot"></span>failed</span>}
        </div>
        {open && (
          <>
            {(turn.tokens_in != null || turn.tokens_out != null) && (
              <div className="turn-meta">
                {turn.tokens_in ?? 0} in · {turn.tokens_out ?? 0} out tokens · {(turn.tool_calls?.length ?? 0)} tool call{(turn.tool_calls?.length ?? 0) === 1 ? "" : "s"}
              </div>
            )}
            {(turn.tool_calls || []).map((tc, i) => (
              <div key={i} className="tool-call">
                <span className="name">{tc.name}</span>
                <span className="arrow">→</span>
                <span className="muted">{typeof tc.args === "string" ? tc.args : JSON.stringify(tc.args)}</span>
                {tc.ok ? <span className="ok">✓ {tc.ms}ms</span> : tc.error ? <span className="fail">✕ {tc.error}</span> : null}
              </div>
            ))}
            {turn.output && (
              <div className="code-block" style={{ marginTop: 6 }}>{turn.output}</div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// =================================================================
// SessionLiveStream — single-session tap live-watch panel
// =================================================================
//
// History + live, re-expressed over the workspace tap (Task 6.2):
//  • HISTORY: GET /v1/sessions/{sid}/messages seeds the full recorded
//    transcript (works for ENDED sessions too), recording a high-water seq.
//  • LIVE: an EventSource opens GET /v1/workspaces/{wid}/tap with a
//    single-session selector (sessions: id == sid) and a resume cursor
//    seeded from the history high-water mark, so the tap tails events with
//    seq > high-water — no gap, no re-replay at the seam. EventSource
//    handles reconnect natively (Last-Event-ID = the cursor), so there is
//    no bespoke backoff loop and no keepalive ping needed.
//  • Tap frames carry `class` (not `kind`) and a nested `payload`; we
//    normalise each into the renderer's flattened {kind, seq, ...payload}
//    shape so _SLS_coalesceMessages / _SLS_Frame render identically.
// Rendering (unchanged):
//  • assistant_token rows are coalesced into one bubble
//  • tool_call / tool_result render as expandable cards
//  • done / cancelled / yielded / resumed render as event markers
//  • error renders as an inline error banner
//  • user_input renders as a user bubble
//  • "Thinking…" appears when turn_status === "running" | "claimable"
// Controls are REST (the stream is read-only):
//  • "Interrupt" → POST /v1/workspaces/{wid}/sessions/{sid}/cancel
//    (the cancel endpoint sets cancel_requested_at + publishes the same
//    session:{sid}:cancel the old WS interrupt frame did).
//  • tool-approval decisions flow through ApprovalBannerPanel's REST
//    respond path; ask_user / yield cancel have their own REST panels.
// Terminal sessions render the full transcript from history and do NOT
// open the tap (nothing left to tail). The panel only mounts if wid is
// known (session row has workspace_id).


// #3/#7: bounded history page. The initial load fetches only the most-recent
// SLS_HISTORY_PAGE recorded rows (a server-side `tail`) instead of the whole
// messages.jsonl (previously limit=1000), so a long session's transcript
// renders immediately and never blocks/times-out on one monolithic payload.
// Older rows load on demand via the "Load earlier" control.
const SLS_HISTORY_PAGE = 200;

function SessionLiveStream({ sid, wid, session, pushToast }) {
  const [messages, setMessages] = React.useState([]);
  // Connection state of the tap EventSource: "connecting" | "open" | "closed".
  const [wsState, setWsState] = React.useState("connecting");
  const [historyLoaded, setHistoryLoaded] = React.useState(false);
  // Whether older recorded rows exist beyond the loaded tail (#3/#7).
  const [moreEarlier, setMoreEarlier] = React.useState(false);
  const [loadingEarlier, setLoadingEarlier] = React.useState(false);
  const { apiFetch } = window.primerApi;
  const scrollRef = React.useRef(null);
  const historyCursorRef = React.useRef(0);
  // How many most-recent recorded rows we've fetched (the tail "offset" for
  // paging older).
  const historyLoadedCountRef = React.useRef(0);

  // Merge a page of recorded rows into `messages`, de-duped by seq + sorted.
  const _mergeHistory = React.useCallback((items) => {
    setMessages((prev) => {
      const seen = new Set(prev.map((p) => p.seq));
      const merged = [...prev];
      for (const it of items) {
        if (seen.has(it.seq)) continue;
        const payload = it.payload && typeof it.payload === "object" ? it.payload : {};
        merged.push({ ...payload, ...it });
      }
      merged.sort((a, b) => (a.seq || 0) - (b.seq || 0));
      return merged;
    });
  }, []);

  React.useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiFetch(
          "GET",
          `/sessions/${encodeURIComponent(sid)}/messages?limit=${SLS_HISTORY_PAGE}&tail=1`,
        );
        if (!alive) return;
        const items = (res && res.items) || [];
        if (items.length) {
          let maxSeq = 0;
          for (const it of items) { if (typeof it.seq === "number" && it.seq > maxSeq) maxSeq = it.seq; }
          // The tail includes the newest rows, so maxSeq is the true global
          // high-water mark — the resume cursor stays gap-free.
          historyCursorRef.current = maxSeq;
          historyLoadedCountRef.current = items.length;
          _mergeHistory(items);
        }
        const total = (res && typeof res.total === "number") ? res.total : items.length;
        setMoreEarlier(total > items.length);
        setHistoryLoaded(true);
      } catch (_e) {
        /* history is best-effort; the tap still tails for live runs */
        if (alive) setHistoryLoaded(true);
      }
    })();
    return () => { alive = false; };
  }, [sid]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Lazily fetch the next older page of recorded rows (#3/#7). Uses the tail
  // offset so paging is anchored to the end of the log, not a shifting start.
  const loadEarlier = React.useCallback(async () => {
    setLoadingEarlier(true);
    try {
      const offset = historyLoadedCountRef.current;
      const res = await apiFetch(
        "GET",
        `/sessions/${encodeURIComponent(sid)}/messages?limit=${SLS_HISTORY_PAGE}&tail=1&offset=${offset}`,
      );
      const items = (res && res.items) || [];
      if (items.length) {
        historyLoadedCountRef.current = offset + items.length;
        _mergeHistory(items);
      }
      const total = (res && typeof res.total === "number") ? res.total : (offset + items.length);
      setMoreEarlier(items.length > 0 && (offset + items.length) < total);
    } catch (_e) {
      /* best-effort — leave older rows unloaded on error */
    } finally {
      setLoadingEarlier(false);
    }
  }, [sid, _mergeHistory]);
  // Whether the session is terminal. A terminal run has no live frames to
  // tail — history already covers the full transcript — so we never open
  // the tap for it. Kept in a ref (seeded from the initial prop, refreshed
  // as the prop changes) so a run that ends mid-stream also stops tailing.
  const terminalRef = React.useRef(!!(session && SESSION_TERMINAL.has(session.status)));
  React.useEffect(() => {
    terminalRef.current = !!(session && SESSION_TERMINAL.has(session.status));
  }, [session?.status]);

  const isRunning = session?.turn_status === "running" || session?.turn_status === "claimable";

  // Live tail via the workspace tap (single-session selector), opened only
  // after history has loaded so the resume cursor can carry the history's
  // high-water seq — the tap then delivers events with seq > that mark, so
  // there is NO gap and NO re-replay at the history↔live seam. A terminal
  // session has nothing left to tail (history is the full transcript) so we
  // skip the connection entirely.
  //
  // EventSource owns reconnect natively (cookie auth + Last-Event-ID = the
  // cursor), so there is no bespoke backoff loop and no keepalive ping. We
  // only mirror its open/error state into the badge. The merge-by-seq dedup
  // below is the seam safety net even though the cursor already excludes the
  // replayed range.
  React.useEffect(() => {
    if (!wid || !sid || !historyLoaded) return undefined;
    if (terminalRef.current) { setWsState("closed"); return undefined; }

    const selector = window.WTP_buildSelector
      ? window.WTP_buildSelector(null, sid)
      : { sessions: { kind: "predicate", left: { kind: "field", name: "id" }, op: "=", right: { kind: "value", value: sid } } };
    // Resume cursor: a single-session seq vector { [sid]: highWater }. The
    // tap decodes this and tails from seq > highWater for this session. When
    // there is no history yet (highWater 0) we omit the cursor so the tap
    // does its own live-from-now init.
    const highWater = historyCursorRef.current || 0;
    const cursorToken = highWater > 0 ? _slsEncodeCursor(sid, highWater) : null;

    let url = `/v1/workspaces/${encodeURIComponent(wid)}/tap?selector=${encodeURIComponent(JSON.stringify(selector))}`;
    if (cursorToken) url += `&cursor=${encodeURIComponent(cursorToken)}`;

    let es;
    try {
      es = new EventSource(url, { withCredentials: true });
    } catch {
      setWsState("closed");
      return undefined;
    }
    setWsState("connecting");

    es.onopen = () => { setWsState("open"); };

    es.onmessage = (ev) => {
      let tev;
      try { tev = JSON.parse(ev.data); } catch { return; }
      if (!tev || typeof tev !== "object") return;
      if (typeof tev.seq !== "number") return;
      // Normalise a TapEvent into the renderer's flattened frame shape:
      // class -> kind, and the nested payload spread onto the top level so
      // _SLS_coalesceMessages / _SLS_Frame read m.text / m.tool_name / etc.
      const payload = tev.payload && typeof tev.payload === "object" ? tev.payload : {};
      const frame = { ...payload, kind: tev.class, seq: tev.seq, payload, ts: tev.ts };
      setMessages((prev) => {
        if (prev.some((p) => p.seq === frame.seq)) return prev;
        return [...prev, frame].sort((a, b) => (a.seq || 0) - (b.seq || 0));
      });
    };

    es.onerror = () => {
      // EventSource auto-reconnects via Last-Event-ID; reflect the drop in
      // the badge. onerror also fires on transient blips, so we do not toast.
      setWsState("closed");
    };

    return () => { try { es.close(); } catch { /* no-op */ } };
  }, [wid, sid, historyLoaded]); // eslint-disable-line react-hooks/exhaustive-deps

  // #10: the embedded stream is PURE CONTENT — no header chrome and no
  // Interrupt control. Session controls (Pause/Resume/Steer/Cancel) live in
  // exactly one place, the parent panel header (ST_SessionControls in
  // studio-center.jsx). The old in-stream "Interrupt" duplicated the header's
  // Cancel and was removed along with the title/frame-count/token-meter/badge.

  // Stick-to-bottom auto-scroll.
  const stickRef = React.useRef(true);
  const onScroll = React.useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = (el.scrollHeight - el.scrollTop - el.clientHeight) < 80;
  }, []);
  React.useEffect(() => {
    if (!scrollRef.current || !stickRef.current) return;
    const el = scrollRef.current;
    const raf = requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
    return () => cancelAnimationFrame(raf);
  }, [messages, isRunning]);

  const coalesced = _SLS_coalesceMessages(messages);
  const isGraph = (session?.binding?.kind || session?.binding_kind) === "graph";

  return (
    <div
      data-testid="session-live-stream"
      style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}
    >
      <div
        ref={scrollRef}
        onScroll={onScroll}
        style={{ flex: 1, overflow: "auto", padding: "14px 18px", minHeight: 120, maxHeight: 480 }}
      >
        {/* #3/#7: page older recorded rows on demand rather than pulling the
            full history at once. */}
        {moreEarlier && (
          <div style={{ textAlign: "center", padding: "2px 0 10px" }}>
            <Btn
              size="sm"
              kind="ghost"
              disabled={loadingEarlier}
              onClick={loadEarlier}
              data-testid="load-earlier"
            >{loadingEarlier ? "Loading…" : "Load earlier"}</Btn>
          </div>
        )}
        {coalesced.length === 0 && (
          terminalRef.current ? (
            <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>
              Session ended — no recorded output at the session level.
              {isGraph ? " See the run view for per-node detail." : ""}
            </div>
          ) : (
            <div className="muted text-sm" style={{ textAlign: "center", padding: 20 }}>
              {wsState === "connecting"
                ? "Connecting to session stream…"
                : wsState === "closed"
                  ? "Stream offline. No frames received or connection dropped."
                  : "No frames yet — session has not started a turn."}
            </div>
          )
        )}
        {coalesced.length > 0 && terminalRef.current && (
          <div className="muted text-sm" style={{ padding: "6px 12px" }}>Session ended</div>
        )}
        {coalesced.map((m, i) =>
          m.kind === "_assistant_message"
            ? <_SLS_Frame key={`am-${m.startSeq}-${m.endSeq}`} m={m} />
            : <_SLS_Frame key={`${m.seq != null ? m.seq : i}-${m.kind}`} m={m} />
        )}
        {isRunning && (
          <div style={{ display: "flex", gap: 12, marginBottom: 14 }} aria-live="polite">
            <div style={{
              width: 52, flexShrink: 0,
              fontFamily: "IBM Plex Mono, monospace", fontSize: 10.5,
              textTransform: "uppercase", letterSpacing: "0.06em",
              color: "var(--accent)", fontWeight: 600, paddingTop: 2,
            }}>agent</div>
            <div style={{
              flex: 1, fontSize: 13, lineHeight: 1.55, color: "var(--text-2)",
              borderLeft: "2px solid var(--accent)", paddingLeft: 12, fontStyle: "italic",
            }}>
              Thinking
              <span className="thinking-dots" style={{ marginLeft: 2 }}>
                <span>.</span><span>.</span><span>.</span>
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

window.SessionLiveStream = SessionLiveStream;

// =================================================================
// SD_GraphRunView - two-pane graph run view (canvas + inspector)
// =================================================================
//
// Left: the shared GR_Canvas (read-only) tinted by per-node status.
// Right: the node inspector (SD_NodeInspector, fleshed out below). Status
// comes from GET /v1/graphs/{gid}/runs/{rid}/node_states, polled every 2s
// (paused once the session is terminal) and refetched immediately on a
// superstep WS event so transitions feel live without a tight poll.

const SD_RUN_STATE_TINT = {
  pending: { border: "var(--border-strong)", glow: null, label: "pending" },
  running: { border: "var(--accent)", glow: "0 0 0 4px var(--accent-dim)", label: "running" },
  waiting: { border: "var(--amber)", glow: "0 0 0 4px var(--amber-dim, transparent)", label: "waiting" },
  ended: { border: "var(--green)", glow: null, label: "ended" },
  failed: { border: "var(--red)", glow: "0 0 0 4px var(--red-dim)", label: "failed" },
};

function SD_overallRunState(items) {
  if (!items.length) return "idle";
  const statuses = items.map((it) => it.status);
  if (statuses.includes("failed")) return "failed";
  if (statuses.includes("running")) return "running";
  if (statuses.includes("waiting")) return "waiting";
  if (statuses.every((s) => s === "ended")) return "ended";
  return "idle";
}

// Read-only run-view canvas: the unified G6 canvas in dagre layout, tinted
// per node by run status. G6 owns layout now (no client-side pre-layout) and the
// status rings live inside its scroll container, so they scroll with the
// nodes and never overflow the page.
function SD_StatusCanvas({ graph, statusByNode, metaByNode, selectedNodeId, onSelectNode }) {
  const draft = React.useMemo(
    () => ({ ...graph, nodes: (graph.nodes || []).map((n) => ({ ...n })), edges: (graph.edges || []).map((e) => ({ ...e })) }),
    [graph],
  );

  const statusTint = React.useMemo(() => {
    const out = {};
    for (const n of (draft.nodes || [])) {
      const st = statusByNode[n.id] || "pending";
      const t = SD_RUN_STATE_TINT[st] || SD_RUN_STATE_TINT.pending;
      out[n.id] = { border: t.border, glow: t.glow, status: st };
    }
    return out;
  }, [draft, statusByNode]);

  return (
    <div style={{ minWidth: 0, overflow: "hidden" }}>
      <window.GR_Canvas
        draft={draft}
        layout="dagre"
        statusTint={statusTint}
        metaByNode={metaByNode}
        selectedNodeId={selectedNodeId}
        selectedEdgeId={null}
        onNodeClick={(id) => onSelectNode(id)}
        onBackgroundClick={() => onSelectNode(null)}
      />
    </div>
  );
}

function SD_GraphRunView({ gid, rid, wid, session, pushToast, onNodeSelect, hideNodeTurnLog, hideInspector }) {
  const { useResource, apiFetch } = window.primerApi;
  const isTerminal = session && window.SESSION_TERMINAL.has(session.status);
  const [selectedNodeId, setSelectedNodeId] = React.useState(null);

  // Node selection keeps its own state (drives the canvas highlight + the
  // inspector) AND, when the OPT-IN onNodeSelect callback is supplied,
  // notifies the caller so it can react to the selection — the Studio graph
  // panel uses this to filter its converged session <Transcript> to the
  // selected node (fix #9). Defaults to today's behavior: no callback ->
  // local-only selection, so the shared export is unaffected when mounted
  // without the opt-in prop.
  const selectNode = React.useCallback((id) => {
    setSelectedNodeId(id);
    if (typeof onNodeSelect === "function") onNodeSelect(id);
  }, [onNodeSelect]);

  const graph = useResource(
    `run-graph-def:${gid}`,
    (s) => apiFetch("GET", `/graphs/${encodeURIComponent(gid)}`, null, { signal: s }),
    { pollMs: 0, deps: [gid] },
  );
  const states = useResource(
    `run-node-states:${rid}`,
    (s) => apiFetch("GET", `/graphs/${encodeURIComponent(gid)}/runs/${encodeURIComponent(rid)}/node_states`, null, { signal: s }),
    {
      pollMs: isTerminal ? 0 : 2000,
      deps: [gid, rid, session?.status],
    },
  );

  // Tap events trigger an immediate refetch so node transitions feel live
  // without a tight poll. We read the SHARED workspace tap
  // (foundation/use-workspace-tap.js) rather than opening a dedicated
  // EventSource — the same single connection that feeds the right-rail
  // Activity + Action Required also drives this refetch. We filter client-side
  // for this run's session and refetch on graph_transition / done / error;
  // a missed frame only delays a refetch and the 2 s poll backstops it. The
  // hook is passed a null wid once terminal so it detaches.
  window.useWorkspaceTapListener(isTerminal ? null : wid, (tev) => {
    if (!tev || typeof tev !== "object" || tev.session_id !== rid) return;
    const cls = tev.class;
    if (cls === "graph_transition" || cls === "done" || cls === "error") {
      states.refetch();
    }
  });

  const items = states.data?.items || [];
  const statusByNode = React.useMemo(() => {
    const out = {};
    for (const it of items) out[it.node_id] = it.status;
    return out;
  }, [items]);
  // Per-node token/duration meta for the canvas badges.
  const metaByNode = React.useMemo(() => {
    const out = {};
    for (const it of items) out[it.node_id] = { tin: it.tokens_in, tout: it.tokens_out, dur: it.duration_ms };
    return out;
  }, [items]);
  const overall = SD_overallRunState(items);
  const supersteps = session?.turn_no ?? session?.turn_count ?? 0;

  if (graph.loading && !graph.data) {
    return <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading graph…</div>;
  }
  if (graph.error && !graph.data) {
    return <Banner kind="error" title="Couldn't load graph" detail={graph.error.detail || graph.error.message} />;
  }

  const selectedItem = items.find((it) => it.node_id === selectedNodeId) || null;

  return (
    <div className="panel" style={{ overflow: "hidden" }}>
      <div className="panel-h">
        <Icon name="graph" size={13} style={{ color: "var(--violet)" }} />
        <span>Run view</span>
        <span className="sub">· superstep {supersteps}</span>
        <div className="right" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className={`pill pill-${overall === "ended" ? "ended" : overall === "failed" ? "failed" : overall === "running" ? "running" : "paused"}`}>
            <span className="dot"></span>{overall}
          </span>
        </div>
      </div>
      {/* fix #9 (studio): when hideInspector is set, the run view is the graph
          canvas alone at full width — the 360px node-event-stream inspector is
          dropped entirely (its content is redundant with the converged session
          transcript the Studio renders below, which filters to the selected
          node via onNodeSelect). The standalone /sessions run view omits the
          prop and keeps the inspector. */}
      <div style={{ display: "grid", gridTemplateColumns: hideInspector ? "minmax(0, 1fr)" : "minmax(0, 1fr) 360px" }}>
        <SD_StatusCanvas
          graph={graph.data}
          statusByNode={statusByNode}
          metaByNode={metaByNode}
          selectedNodeId={selectedNodeId}
          onSelectNode={selectNode}
        />
        {!hideInspector && (
          <SD_NodeInspector
            gid={gid}
            rid={rid}
            wid={wid}
            session={session}
            node={selectedItem}
            graph={graph.data}
            pushToast={pushToast}
            hideNodeTurnLog={hideNodeTurnLog}
          />
        )}
      </div>
    </div>
  );
}

// Short kind-aware descriptions shown in the inspector so the operator
// knows what each node does without opening the graph editor.
const SD_NODE_KIND_HINT = {
  agent: "agent turn — runs an agent to completion",
  begin: "begin — graph entry point",
  end: "end — emits the graph's structured output",
  tool_call: "tool call — invokes a single tool",
  fan_out: "fan-out — spawns parallel branches",
  fan_in: "fan-in — joins parallel branches",
  graph: "subgraph — delegates to another graph",
};

// Per-node turn-log tail (existing endpoint). Read-on-completion: polls
// while the node is non-terminal, stops once ended/failed.
function SD_NodeTurnLog({ gid, rid, nodeId, nodeStatus }) {
  const { useResource, apiFetch } = window.primerApi;
  const terminal = nodeStatus === "ended" || nodeStatus === "failed";
  const log = useResource(
    `run-node-turnlog:${rid}:${nodeId}`,
    (s) => apiFetch(
      "GET",
      `/graphs/${encodeURIComponent(gid)}/runs/${encodeURIComponent(rid)}/nodes/${encodeURIComponent(nodeId)}/turn_log?limit=200`,
      null,
      { signal: s },
    ),
    { pollMs: terminal ? 0 : 4000, deps: [gid, rid, nodeId, nodeStatus] },
  );
  const items = log.data?.items || [];
  if (log.error?.status === 404 || items.length === 0) {
    return (
      <div className="muted text-sm" style={{ padding: 12 }}>
        no activity yet for this node
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: 12 }}>
      {items.map((e, i) => <TurnLogRow key={`${e.seq}-${i}`} e={e} />)}
    </div>
  );
}

function SD_NodeInspector({ gid, rid, wid, session, node, graph, pushToast, hideNodeTurnLog }) {
  const { useRouter } = window.primerApi;
  const { navigate } = useRouter();
  const nodeId = node && node.node_id;

  // Node-attributed output via the workspace tap (node-scoped, gap-free):
  // mirrors SessionLiveStream's history+cursor+lifecycle seam exactly.
  //  • HISTORY: GET .../sessions/{rid}/messages seeds the full recorded
  //    transcript; we filter for this node's frames but compute the
  //    high-water seq across ALL fetched records (not just node's) so the
  //    resume cursor is correct and covers the full session's seq space.
  //  • LIVE: EventSource on GET /v1/workspaces/{wid}/tap with a selector
  //    combining sessions:id==rid AND events:node_id==nodeId, resuming
  //    from cursor {seqs:{rid:maxSeq}} — no gap, no re-replay at the seam.
  // Hooks run unconditionally (before the no-selection early return) so
  // the hook order is stable across node selection. v1 shows the turn-log
  // + node-attributed session frames; token-live per-node output is the
  // documented fast-follow (spec §8).
  const [frames, setFrames] = React.useState([]);
  const _niHistoryLoaded = React.useRef(false);
  const _niCursorRef = React.useRef(0);
  React.useEffect(() => {
    setFrames([]);
    _niHistoryLoaded.current = false;
    _niCursorRef.current = 0;
    if (!wid || !rid || !nodeId) return undefined;
    let alive = true;
    let es;
    const openTap = () => {
      if (!alive) return;
      // Build a selector: sessions:id==rid AND events:node_id==nodeId.
      const selector = {
        sessions: { kind: "predicate", left: { kind: "field", name: "id" }, op: "=", right: { kind: "value", value: rid } },
        events: { kind: "predicate", left: { kind: "field", name: "node_id" }, op: "=", right: { kind: "value", value: nodeId } },
      };
      const highWater = _niCursorRef.current || 0;
      const cursorToken = highWater > 0 ? _slsEncodeCursor(rid, highWater) : null;
      let url = `/v1/workspaces/${encodeURIComponent(wid)}/tap?selector=${encodeURIComponent(JSON.stringify(selector))}`;
      if (cursorToken) url += `&cursor=${encodeURIComponent(cursorToken)}`;
      try {
        es = new EventSource(url, { withCredentials: true });
      } catch { return; }
      es.onmessage = (ev) => {
        let tev;
        try { tev = JSON.parse(ev.data); } catch { return; }
        if (!tev || typeof tev !== "object" || typeof tev.seq !== "number") return;
        const payload = tev.payload && typeof tev.payload === "object" ? tev.payload : {};
        const frame = { ...payload, kind: tev.class, seq: tev.seq, payload, ts: tev.ts };
        // Defensive node filter: the server-side selector already gates this
        // but we double-check so a selector mismatch can't pollute frames.
        const fnode = frame.node_id || frame.end_node_id;
        if (fnode !== nodeId) return;
        setFrames((prev) => prev.some((p) => p.seq === frame.seq) ? prev : [...prev, frame].sort((a, b) => (a.seq || 0) - (b.seq || 0)));
      };
    };
    // Seed history then open the tap (mirrors SessionLiveStream seam).
    (async () => {
      try {
        const res = await window.primerApi.apiFetch("GET", `/sessions/${encodeURIComponent(rid)}/messages?limit=1000`);
        if (!alive) return;
        const items = (res && res.items) || [];
        if (items.length) {
          // High-water across ALL fetched records (not just this node's) so
          // the tap cursor covers the full session seq space — same logic as
          // SessionLiveStream lines 823-825.
          let maxSeq = 0;
          for (const it of items) { if (typeof it.seq === "number" && it.seq > maxSeq) maxSeq = it.seq; }
          _niCursorRef.current = maxSeq;
          // Keep only frames attributed to this node for display.
          const nodeItems = items.filter((it) => {
            const p = it.payload && typeof it.payload === "object" ? it.payload : {};
            return (it.node_id || p.node_id || it.end_node_id || p.end_node_id) === nodeId;
          });
          if (nodeItems.length) {
            setFrames((prev) => {
              const seen = new Set(prev.map((p) => p.seq));
              const merged = [...prev];
              for (const it of nodeItems) {
                if (seen.has(it.seq)) continue;
                const payload = it.payload && typeof it.payload === "object" ? it.payload : {};
                merged.push({ ...payload, ...it });
              }
              return merged.sort((a, b) => (a.seq || 0) - (b.seq || 0));
            });
          }
        }
      } catch { /* history is best-effort; tap still tails for live runs */ }
      if (alive) openTap();
    })();
    return () => {
      alive = false;
      try { if (es) es.close(); } catch { /* no-op */ }
    };
  }, [wid, rid, nodeId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Drop graph_transition frames from the DISPLAYED stream — they render as
  // a wall of bare "graph_transition" lines with no operator value. A frame's
  // class/kind lives on kind (tap frames normalise tev.class -> kind; history
  // records carry it.kind), or on class / payload.class; match the same way
  // the refetch tap listener detects it (cls === "graph_transition"). NOTE:
  // this only filters what is rendered — the useWorkspaceTapListener refetch
  // trigger above is untouched.
  const visibleFrames = frames.filter((f) => {
    const cls = (f && f.kind) || (f && f.class) || (f && f.payload && f.payload.class);
    return cls !== "graph_transition";
  });
  const coalesced = window._SLS_coalesceMessages(visibleFrames);

  // Empty state: no node selected -> the session-level live stream (the
  // run's End output), so the default view still shows the run's result.
  if (!node) {
    return (
      <div style={{ borderLeft: "1px solid var(--border)", minHeight: 500 }}>
        {wid
          ? <SessionLiveStream sid={rid} wid={wid} session={session} pushToast={pushToast} />
          : <div className="muted text-sm" style={{ textAlign: "center", padding: 30 }}>
              Select a node to inspect it.
            </div>}
      </div>
    );
  }

  const def = (graph?.nodes || []).find((n) => n.id === node.node_id) || {};
  const tint = window.SD_RUN_STATE_TINT[node.status] || window.SD_RUN_STATE_TINT.pending;

  // Subgraph node: a link to drill into the subgraph definition.
  const subgraphLink = node.kind === "graph" && def.graph_id ? (
    <div style={{ padding: 12 }}>
      <a style={{ color: "var(--violet)", cursor: "pointer" }}
        onClick={() => navigate("/graphs/" + def.graph_id)}>
        Open subgraph {def.graph_id} →
      </a>
    </div>
  ) : null;

  return (
    <div style={{ borderLeft: "1px solid var(--border)", minHeight: 500, display: "flex", flexDirection: "column" }}>
      <div className="panel-h" style={{ borderBottom: `2px solid ${tint.border}` }}>
        <span className="mono" style={{ fontWeight: 600 }}>{node.node_id}</span>
        <span className="muted text-sm">· {node.kind}</span>
        {node.kind === "agent" && def.agent_id && (
          <a style={{ color: "var(--accent)", cursor: "pointer" }}
            onClick={() => navigate("/agents/" + def.agent_id)}>{def.agent_id}</a>
        )}
        <div className="right">
          <span className={`pill pill-${node.status === "ended" ? "ended" : node.status === "failed" ? "failed" : node.status === "running" ? "running" : "paused"}`}>
            <span className="dot"></span>{node.status}
          </span>
        </div>
      </div>
      <div style={{ flex: 1, overflow: "auto", maxHeight: 480 }}>
        <div className="muted text-sm" style={{ padding: "10px 14px 2px" }}>
          {SD_NODE_KIND_HINT[node.kind] || node.kind}
        </div>
        {node.status === "failed" && node.error && (
          <div style={{ padding: 12 }}>
            <window._SLS_NodeErrorBadge error={node.error} code={null} />
          </div>
        )}
        {/* Kind-aware stream/output via the SHARED frame renderer. */}
        {subgraphLink}
        {node.kind !== "graph" && coalesced.length > 0 && (
          <div style={{ padding: "12px 14px" }}>
            {coalesced.map((m, i) => (
              <window._SLS_Frame key={m.seq != null ? m.seq : i} m={m} />
            ))}
          </div>
        )}
        {/* Per-node turn-log below the stream. Suppressed when the caller
            opts into hideNodeTurnLog (the Studio graph panel, which converges
            per-node detail into its filtered session <Transcript> instead —
            fix #9). Defaults to shown, so the standalone run-view export is
            unaffected. */}
        {!hideNodeTurnLog && (
          <div style={{ borderTop: "1px solid var(--border)" }}>
            <div className="muted text-sm" style={{ padding: "8px 12px 0", textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 10.5 }}>
              Turn log
            </div>
            <SD_NodeTurnLog gid={gid} rid={rid} nodeId={node.node_id} nodeStatus={node.status} />
          </div>
        )}
      </div>
    </div>
  );
}

window.SD_GraphRunView = SD_GraphRunView;
window.SD_RUN_STATE_TINT = SD_RUN_STATE_TINT;
window.SD_NodeInspector = SD_NodeInspector;
window.SD_NodeTurnLog = SD_NodeTurnLog;

// =================================================================
// Yielding-tools UI surfaces
// =================================================================

// AskUserPanel — polls GET /v1/sessions/{sid}/ask_user/pending (200 =
// render; 404 = render nothing). Submit/Skip post to the real
// endpoints; 422/500 are surfaced INLINE via data-testid="ask-user-error"
// (U0051/U0060), success surfaces as a toast (U0049/U0050).
function AskUserPanel({ sid, sessionStatus, session, pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const isTerminal = SESSION_TERMINAL.has(sessionStatus);

  const pending = useResource(
    `ask-user:${sid}`,
    (signal) => apiFetch("GET", `/sessions/${encodeURIComponent(sid)}/ask_user/pending`, null, { signal }),
    {
      pollMs: isTerminal ? 0 : 2000,
      deps: [sid, sessionStatus],
    }
  );

  const [draft, setDraft] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [skipping, setSkipping] = React.useState(false);
  const [inlineError, setInlineError] = React.useState(null);

  // Clear local edit state when the prompt id changes.
  const tcid = pending.data?.tool_call_id;
  React.useEffect(() => {
    setDraft("");
    setInlineError(null);
  }, [tcid]);

  if (pending.error?.status === 404) return null;
  if (!pending.data) return null;

  const { prompt, response_schema, parked_at } = pending.data;
  const expectsJson = response_schema && response_schema.type === "object";
  const isShortPrompt = !prompt.includes("\n") && prompt.length <= 80;

  const onSubmit = async () => {
    if (!draft.trim()) return;
    setSubmitting(true);
    setInlineError(null);
    let response = draft;
    if (expectsJson) {
      try {
        response = JSON.parse(draft);
      } catch (e) {
        setInlineError("Response must be valid JSON: " + e.message);
        setSubmitting(false);
        return;
      }
    }
    try {
      await apiFetch(
        "POST",
        `/sessions/${encodeURIComponent(sid)}/ask_user/respond`,
        { tool_call_id: tcid, response },
      );
      if (pushToast) pushToast({ kind: "success", title: "Response sent", detail: "Session resuming." });
      setDraft("");
      pending.refetch();
    } catch (err) {
      // 422 + 500 + anything else: inline (NEVER toast). U0051/U0060.
      setInlineError(err.detail || err.title || err.message || "Submit failed");
    } finally {
      setSubmitting(false);
    }
  };

  const onSkip = async () => {
    setSkipping(true);
    setInlineError(null);
    try {
      await apiFetch(
        "POST",
        `/sessions/${encodeURIComponent(sid)}/yields/${encodeURIComponent(tcid)}/cancel`,
        { reason: "operator skipped" },
      );
      if (pushToast) pushToast({
        kind: "warning",
        title: "Skipped",
        detail: "Agent will continue without your input.",
      });
      setDraft("");
      pending.refetch();
    } catch (err) {
      setInlineError(err.detail || err.title || err.message || "Skip failed");
    } finally {
      setSkipping(false);
    }
  };

  const placeholder = expectsJson ? "JSON object…" : "";

  return (
    <div className="panel" style={{ borderColor: "oklch(0.7 0.18 240 / 0.4)" }} data-testid="ask-user-panel">
      <div className="panel-h" style={{ background: "var(--blue-dim, transparent)" }}>
        <Icon name="info" size={13} />
        <span>Input requested</span>
        {parked_at && (
          <span className="sub muted">· waiting since {fmtDate(new Date(parked_at))}</span>
        )}
        <window.SessionCountdown to={pending?.data?.parked_until || pending?.data?.timeout_at || session?.parked_until} prefix="auto-resumes in " />
      </div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ whiteSpace: "pre-wrap", color: "var(--text)" }}>{prompt}</div>
        {isShortPrompt && !expectsJson ? (
          <input
            type="text"
            className="textarea"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSubmit();
              }
            }}
            placeholder=""
            disabled={submitting || skipping}
            data-testid="ask-user-input"
          />
        ) : (
          <textarea
            className={"textarea" + (expectsJson ? " mono" : "")}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={placeholder}
            rows={expectsJson ? 6 : 4}
            disabled={submitting || skipping}
            data-testid="ask-user-textarea"
          />
        )}
        {inlineError && (
          <div className="text-sm" style={{ color: "var(--red)" }} data-testid="ask-user-error">
            {inlineError}
          </div>
        )}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Btn
            kind="ghost"
            onClick={onSkip}
            disabled={submitting || skipping}
            data-testid="ask-user-skip"
          >
            Skip this prompt
          </Btn>
          <Btn
            kind="primary"
            onClick={onSubmit}
            disabled={!draft.trim() || submitting || skipping}
            data-testid="ask-user-submit"
          >
            {submitting ? "Sending…" : "Send response"}
          </Btn>
        </div>
      </div>
    </div>
  );
}

// WatchFilesPanel — polls /yields/active for this session and renders
// the watch_files-yield panel when one is parked. Cancel posts the
// tool-agnostic yield-cancel endpoint.
function WatchFilesPanel({ sid, wid, session, pushToast }) {
  // The session row's parked_state (when present) is the authoritative
  // signal — the server keeps it in sync with the yield row. We rely on
  // it directly rather than polling a separate endpoint.
  const yld = session?.parked_state?.yielded;
  const toolName = yld?.tool_name || (session?.parked_state ? session.parked_state.tool_name : null);
  const isParked = session?.parked_status === "parked" || session?.parked_status === "waiting";
  if (!isParked || toolName !== "watch_files") return null;

  const tcid = session?.parked_state?.tool_call_id || yld?.tool_call_id;
  const meta = yld?.resume_metadata || yld?.metadata || {};
  const paths = meta.paths || [];
  const win = meta.coalesce_window_ms;
  const parkedAt = session?.parked_state?.parked_at;
  const parkedSec = parkedAt ? _sdAgeSec(parkedAt) : null;

  return (
    <div className="panel" data-testid="watch-files-panel">
      <div className="panel-h">
        <Icon name="search" size={13} style={{ color: "var(--amber)" }} />
        <span style={{ color: "var(--amber)" }}>Watching</span>
        <span className="mono sub">· watch_files · {tcid}</span>
        <div className="right">
          <CancelYieldBtn sid={sid} wid={wid} tcid={tcid} pushToast={pushToast} />
        </div>
      </div>
      <div className="panel-body">
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "6px 16px" }}>
          <span className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 10.5, paddingTop: 4 }}>paths</span>
          <div className="col" style={{ gap: 3 }}>
            {paths.map((p, i) => (
              <div key={i} className="mono" style={{ fontSize: 12.5 }}>
                <Icon name="doc" size={11} style={{ verticalAlign: -1, color: "var(--text-3)", marginRight: 4 }} />
                {p}
              </div>
            ))}
          </div>
          {win != null && (<>
            <span className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 10.5 }}>coalesce</span>
            <span className="mono">{win}ms</span>
          </>)}
          {parkedSec != null && (<>
            <span className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 10.5 }}>parked</span>
            <span className="mono">{relativeTime(parkedSec)}</span>
          </>)}
        </div>
      </div>
    </div>
  );
}

// SleepPanel — same parked_state-driven approach as WatchFilesPanel.
function SleepPanel({ sid, wid, session, pushToast }) {
  const yld = session?.parked_state?.yielded;
  const toolName = yld?.tool_name || (session?.parked_state ? session.parked_state.tool_name : null);
  const isParked = session?.parked_status === "parked" || session?.parked_status === "waiting";
  if (!isParked || toolName !== "sleep") return null;

  const tcid = session?.parked_state?.tool_call_id || yld?.tool_call_id;
  const meta = yld?.resume_metadata || yld?.metadata || {};
  const duration = meta.duration_s || 0;
  const resumeAt = meta.resume_at ? new Date(meta.resume_at) : null;
  const parkedAt = session?.parked_state?.parked_at;
  const elapsed = parkedAt ? (Date.now() - new Date(parkedAt).getTime()) / 1000 : 0;
  const remaining = Math.max(0, duration - elapsed);
  const pct = Math.min(100, (elapsed / Math.max(1, duration)) * 100);

  return (
    <div className="panel" data-testid="sleep-panel">
      <div className="panel-h">
        <Icon name="clock" size={13} style={{ color: "var(--amber)" }} />
        <span style={{ color: "var(--amber)" }}>Sleeping</span>
        <span className="mono sub">· sleep · {tcid}</span>
        <div className="right">
          <CancelYieldBtn sid={sid} wid={wid} tcid={tcid} pushToast={pushToast} />
        </div>
      </div>
      <div className="panel-body">
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 6 }}>
          <div className="mono tabular" style={{ fontSize: 22, fontWeight: 600 }}>
            {Math.floor(remaining / 60).toString().padStart(2, "0")}:{Math.floor(remaining % 60).toString().padStart(2, "0")}
            <span className="muted text-sm" style={{ marginLeft: 6 }}>remaining</span>
          </div>
          <div className="muted text-sm mono tabular">duration {duration}s · resume {resumeAt ? fmtDate(resumeAt).slice(11) : "—"}</div>
        </div>
        <div style={{ height: 6, background: "var(--bg-2)", borderRadius: 3, overflow: "hidden" }}>
          <div style={{ width: `${pct}%`, height: "100%", background: "var(--amber)", transition: "width 1s linear" }}></div>
        </div>
        <window.SessionCountdown to={meta.resume_at || session?.parked_until} prefix="auto-resumes in " />
      </div>
    </div>
  );
}

// Shared cancel-yield button (WatchFiles + Sleep panels).
function CancelYieldBtn({ sid, wid, tcid, pushToast }) {
  const { useMutation, apiFetch } = window.primerApi;
  const cancel = useMutation(
    () => apiFetch(
      "POST",
      `/sessions/${encodeURIComponent(sid)}/yields/${encodeURIComponent(tcid)}/cancel`,
      { reason: "operator cancelled" },
    ),
    {
      invalidates: [`session-detail:${sid}`, `ask-user:${sid}`],
      onSuccess: () => pushToast && pushToast({
        kind: "warning",
        title: "Yield cancelled",
        detail: "Agent will continue.",
      }),
      onError: _sdToastErr(pushToast, "Cancel failed"),
    }
  );
  return (
    <Btn size="sm" kind="ghost" icon="x" disabled={cancel.loading || !tcid} onClick={() => cancel.mutate()}>
      Cancel yield
    </Btn>
  );
}

// ApprovalBannerPanel — polls GET /v1/sessions/{sid}/tool_approval/pending
// (200 = render banner; 404 = render nothing) and renders ApprovalBanner
// from approvals.jsx. The banner owns the respond mutation; this wrapper
// owns the poll so session-detail can keep wiring concerns local.
function ApprovalBannerPanel({ sid, sessionStatus, session, pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const isTerminal = SESSION_TERMINAL.has(sessionStatus);
  const pending = useResource(
    `tool-approval:session:${sid}`,
    (signal) => apiFetch("GET", `/sessions/${encodeURIComponent(sid)}/tool_approval/pending`, null, { signal }),
    {
      pollMs: isTerminal ? 0 : 2000,
      deps: [sid, sessionStatus],
    },
  );
  if (pending.error?.status === 404) return null;
  if (!pending.data) return null;
  return (
    <div>
      <ApprovalBanner data={pending.data} scope="sessions" id={sid} pushToast={pushToast} />
      <window.SessionCountdown to={pending.data?.parked_until || pending.data?.timeout_at || session?.parked_until} prefix="expires in " />
    </div>
  );
}

// =============================================================================
// Turn log tab + workspace-correlation chip
// =============================================================================

const _TURN_LOG_KIND_META = {
  started:           { color: "var(--blue, #38bdf8)",   label: "STARTED" },
  completed:         { color: "var(--green, #4ade80)",  label: "COMPLETED" },
  failed:            { color: "var(--red, #ef4444)",    label: "FAILED" },
  yielded:           { color: "var(--amber, #fbbf24)",  label: "YIELDED" },
  resumed:           { color: "var(--violet, #a78bfa)", label: "RESUMED" },
  cancelled:         { color: "var(--text-3, #9ca3af)", label: "CANCELLED" },
  superstep_started: { color: "var(--blue, #38bdf8)",   label: "SUPERSTEP START" },
  superstep_ended:   { color: "var(--green, #4ade80)",  label: "SUPERSTEP END" },
};

function TurnLogRow({ e }) {
  const [expanded, setExpanded] = React.useState(false);
  const meta = _TURN_LOG_KIND_META[e.kind] || { color: "var(--text-3)", label: (e.kind || "").toUpperCase() };
  const onToggle = () => setExpanded((v) => !v);
  return (
    <div
      onClick={onToggle}
      style={{
        padding: "8px 12px",
        border: "1px solid var(--border)",
        borderLeft: `3px solid ${meta.color}`,
        borderRadius: 4,
        background: "var(--bg-1, var(--bg))",
        cursor: "pointer",
        fontSize: 12,
      }}
    >
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{
          fontSize: 10, fontWeight: 700, color: meta.color,
          letterSpacing: "0.06em",
        }}>{meta.label}</span>
        <span className="muted text-sm">seq {e.seq}</span>
        {e.node_id && <span className="muted text-sm mono">node: {e.node_id}</span>}
        {typeof e.iteration === "number" && (
          <span className="muted text-sm">iter {e.iteration}</span>
        )}
        {typeof e.duration_ms === "number" && (
          <span className="muted text-sm">{e.duration_ms} ms</span>
        )}
        {typeof e.input_tokens === "number" && (
          <span className="muted text-sm">in {e.input_tokens}</span>
        )}
        {typeof e.output_tokens === "number" && (
          <span className="muted text-sm">out {e.output_tokens}</span>
        )}
        {e.kind === "yielded" && (
          <span className="muted text-sm mono">{e.yield_kind}: {e.event_key}</span>
        )}
        {e.kind === "resumed" && typeof e.wait_ms === "number" && (
          <span className="muted text-sm">waited {e.wait_ms} ms</span>
        )}
        {e.kind === "failed" && e.error && (
          <span style={{ color: "var(--red)" }}>{e.error.title}</span>
        )}
        {e.ts && <span className="muted text-sm" style={{ marginLeft: "auto" }}>{e.ts}</span>}
      </div>
      {e.kind === "failed" && e.error && !expanded && (
        <div className="muted text-sm" style={{ marginTop: 4 }}>
          {e.error.detail}
        </div>
      )}
      {expanded && (
        <pre style={{
          fontSize: 11, marginTop: 8, padding: 8,
          background: "var(--bg-0, var(--bg))",
          border: "1px solid var(--border)",
          borderRadius: 4, overflow: "auto", maxHeight: 240,
          fontFamily: "IBM Plex Mono, monospace",
        }}>{JSON.stringify(e, null, 2)}</pre>
      )}
    </div>
  );
}

function _turnLogEndpoint({ sessionId, binding, scope }) {
  // For agent sessions: /sessions/{sid}/turn_log
  // For graph sessions: /graphs/{gid}/runs/{sid}/turn_log (scope=graph)
  //                 or  /graphs/{gid}/runs/{sid}/nodes/{nid}/turn_log (scope=node:<nid>)
  const isGraph = binding?.kind === "graph";
  if (!isGraph) {
    return `/sessions/${encodeURIComponent(sessionId)}/turn_log?limit=200`;
  }
  const gid = encodeURIComponent(binding.graph_id);
  const rid = encodeURIComponent(sessionId);
  if (typeof scope === "string" && scope.startsWith("node:")) {
    const nid = encodeURIComponent(scope.slice("node:".length));
    return `/graphs/${gid}/runs/${rid}/nodes/${nid}/turn_log?limit=200`;
  }
  return `/graphs/${gid}/runs/${rid}/turn_log?limit=200`;
}

function TurnLogTab({ sessionId, sessionStatus, binding }) {
  const { useResource, apiFetch } = window.primerApi;
  const isTerminal = SESSION_TERMINAL.has(sessionStatus);
  const isGraph = binding?.kind === "graph";
  // For graph runs, expose a small scope switcher: graph-level events
  // (default) or a single node's events. The node list is derived from
  // the graph-level events themselves (every node_id we've seen).
  const [scope, setScope] = React.useState("graph");
  const path = _turnLogEndpoint({ sessionId, binding, scope });
  const cacheKey = `session-turn-log:${sessionId}:${scope}`;
  const log = useResource(
    cacheKey,
    (signal) => apiFetch("GET", path, null, { signal }),
    {
      pollMs: isTerminal ? 0 : 5000,
      deps: [sessionId, sessionStatus, scope],
    },
  );

  const items = log.data?.items || [];

  // Build the set of known node ids from the items so the picker is
  // populated as soon as we have at least one event. This hook MUST run
  // on every render (before any early return) so hook order stays stable
  // across the loading -> loaded transition (React error #310 otherwise).
  const nodeIds = React.useMemo(() => {
    if (!isGraph) return [];
    const set = new Set();
    for (const ev of items) {
      if (ev.node_id) set.add(ev.node_id);
      if (Array.isArray(ev.ready_node_ids)) {
        for (const nid of ev.ready_node_ids) set.add(nid);
      }
      if (Array.isArray(ev.completed_node_ids)) {
        for (const nid of ev.completed_node_ids) set.add(nid);
      }
      if (Array.isArray(ev.failed_node_ids)) {
        for (const nid of ev.failed_node_ids) set.add(nid);
      }
    }
    return Array.from(set).sort();
  }, [items, isGraph]);

  if (log.loading && !log.data) {
    return <div className="muted text-sm" style={{ padding: 16 }}>Loading turn log…</div>;
  }
  if (log.error) {
    return <Banner kind="error" title="Could not load turn log" detail={log.error.message || log.error.title || ""} />;
  }

  const scopePicker = isGraph ? (
    <div style={{ display: "flex", gap: 6, alignItems: "center", padding: "0 12px 8px" }}>
      <span className="muted text-sm">Scope:</span>
      <Btn
        size="sm"
        kind={scope === "graph" ? "primary" : "ghost"}
        onClick={() => setScope("graph")}
      >Graph-level</Btn>
      {nodeIds.map((nid) => (
        <Btn
          key={nid}
          size="sm"
          kind={scope === `node:${nid}` ? "primary" : "ghost"}
          onClick={() => setScope(`node:${nid}`)}
        >{nid}</Btn>
      ))}
    </div>
  ) : null;

  if (items.length === 0) {
    return (
      <div className="col">
        {scopePicker}
        <div className="muted text-sm" style={{ padding: 16 }}>
          No turn-log entries yet. Events are written as the session runs.
        </div>
      </div>
    );
  }
  return (
    <div className="col">
      {scopePicker}
      <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: 12 }}>
        {items.map((e, i) => <TurnLogRow key={`${e.seq}-${i}`} e={e} />)}
      </div>
    </div>
  );
}

function WorkspaceFailureChip({ workspaceId }) {
  const { useResource, apiFetch } = window.primerApi;
  const ws = useResource(
    `ws-fail:${workspaceId}`,
    (signal) => apiFetch("GET", `/workspaces/${encodeURIComponent(workspaceId)}`, null, { signal }),
    { pollMs: 0, deps: [workspaceId] },
  );
  const failureReason = ws.data?.failure_reason;
  if (!failureReason) return null;
  return (
    <div style={{
      marginTop: 8, padding: "8px 12px",
      borderLeft: "3px solid var(--red)",
      background: "var(--bg-2, var(--bg))",
      borderRadius: 4, fontSize: 12,
    }}>
      <div className="muted text-sm" style={{ marginBottom: 4 }}>
        Workspace <span className="mono">{workspaceId}</span> failure_reason:
      </div>
      <div className="mono" style={{ wordBreak: "break-word" }}>
        {failureReason}
      </div>
    </div>
  );
}

window.AskUserPanel = AskUserPanel;
window.WatchFilesPanel = WatchFilesPanel;
window.SleepPanel = SleepPanel;
window.ApprovalBannerPanel = ApprovalBannerPanel;
window.TurnLogTab = TurnLogTab;
window.TurnLogRow = TurnLogRow;
window.WorkspaceFailureChip = WorkspaceFailureChip;

// =================================================================
// Graph health (replaces the old hardcoded reference-status pill)
// =================================================================

// Real reference-resolution state from GET /v1/graphs/{id}/status.
function SD_GraphHealthPanel({ gid }) {
  const { useResource, apiFetch } = window.primerApi;
  const status = useResource(
    `graph-status:${gid}`,
    (s) => apiFetch("GET", `/graphs/${encodeURIComponent(gid)}/status`, null, { signal: s }),
    { pollMs: 30000, deps: [gid] },
  );
  const ok = status.data?.ok;
  const issues = status.data?.issues || [];
  return (
    <div className="ref-row" style={{ alignItems: "flex-start", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Icon name={ok === true ? "check-circle" : ok === false ? "x-circle" : "info"} size={13}
          style={{ color: ok === true ? "var(--green)" : ok === false ? "var(--red)" : "var(--text-3)" }} />
        <span className="label" style={{ color: ok === false ? "var(--red)" : undefined }}>
          {ok === true ? "All references resolve" : ok === false ? `${issues.length} issue${issues.length === 1 ? "" : "s"}` : "Checking references…"}
        </span>
      </div>
      {ok === false && issues.map((iss, i) => (
        <div key={i} className="muted text-sm mono" style={{ color: "var(--red)", paddingLeft: 19 }}>{iss}</div>
      ))}
    </div>
  );
}
window.SD_GraphHealthPanel = SD_GraphHealthPanel;

// Prominent body banner: "This graph cannot run: <first issue>" with the
// full list expandable; or a neutral why-idle hint when ok-but-stuck.
function SD_CannotRunBanner({ gid, session }) {
  const { useResource, apiFetch } = window.primerApi;
  const [open, setOpen] = React.useState(false);
  const status = useResource(
    `graph-status:${gid}`,
    (s) => apiFetch("GET", `/graphs/${encodeURIComponent(gid)}/status`, null, { signal: s }),
    { pollMs: 30000, deps: [gid] },
  );
  const ok = status.data?.ok;
  const issues = status.data?.issues || [];
  const turn = session?.turn_no ?? session?.turn_count ?? 0;
  const lastError = session?.last_error || session?.error;

  if (ok === false && issues.length) {
    return (
      <div className="panel" style={{ borderColor: "oklch(0.7 0.2 25 / 0.5)" }} data-testid="graph-cannot-run">
        <div className="panel-h" style={{ background: "var(--red-dim)", cursor: issues.length > 1 ? "pointer" : "default" }}
          onClick={() => issues.length > 1 && setOpen((v) => !v)}>
          {issues.length > 1 && <Icon name={open ? "chevron-down" : "chevron-right"} size={12} style={{ color: "var(--red)" }} />}
          <Icon name="x-circle" size={13} style={{ color: "var(--red)" }} />
          <span style={{ color: "var(--red)" }}>This graph cannot run: {issues[0]}</span>
        </div>
        {open && (
          <div className="panel-body">
            {issues.map((iss, i) => (
              <div key={i} className="muted text-sm mono" style={{ color: "var(--red)" }}>{iss}</div>
            ))}
          </div>
        )}
      </div>
    );
  }

  // Why-idle: running, turn 0, references ok, no error -> neutral hint.
  if (ok === true && session?.status === "running" && turn === 0 && !lastError) {
    return (
      <div className="panel" data-testid="graph-waiting-start">
        <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 14px" }}>
          <Icon name="info" size={13} className="muted" />
          <span className="muted text-sm">Waiting to start its first turn.</span>
        </div>
      </div>
    );
  }
  return null;
}
window.SD_CannotRunBanner = SD_CannotRunBanner;

// =================================================================
// Agent-session light additions (spec §7)
// =================================================================

// Status line derived from session status + the latest turn-log record.
function SD_AgentStatusLine({ session, sid }) {
  const { useResource, apiFetch } = window.primerApi;
  const isTerminal = SESSION_TERMINAL.has(session?.status);
  const log = useResource(
    `agent-status-tail:${sid}`,
    (s) => apiFetch("GET", `/sessions/${encodeURIComponent(sid)}/turn_log?limit=1&offset=0`, null, { signal: s }),
    { pollMs: isTerminal ? 0 : 5000, deps: [sid, session?.status] },
  );
  const last = (log.data?.items || [])[0];
  let label = session?.status || "unknown";
  let color = "var(--text-2)";
  if (session?.status === "running") { label = "running"; color = "var(--accent)"; }
  else if (session?.status === "paused") { label = "paused"; color = "var(--amber)"; }
  else if (session?.status === "failed") { label = "failed"; color = "var(--red)"; }
  else if (session?.status === "ended" || session?.status === "completed") { label = "ended"; color = "var(--green)"; }
  if (last?.kind === "yielded" && last.yield_kind) {
    label = `waiting on ${last.yield_kind}`;
    color = "var(--amber)";
  }
  return (
    <div className="text-sm mono" data-testid="agent-status-line"
      style={{ color, display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
      <span className="dot" style={{ background: color, width: 7, height: 7, borderRadius: "50%", display: "inline-block" }}></span>
      {label}
    </div>
  );
}
window.SD_AgentStatusLine = SD_AgentStatusLine;

// Compact turn timeline: one TurnRow per turn-log record. Clicking a turn
// is a future scroll-to-turn hook; v1 renders the expandable rows.
function SD_AgentTurnTimeline({ sid, session }) {
  const { useResource, apiFetch } = window.primerApi;
  const isTerminal = SESSION_TERMINAL.has(session?.status);
  const log = useResource(
    `agent-turn-timeline:${sid}`,
    (s) => apiFetch("GET", `/sessions/${encodeURIComponent(sid)}/turn_log?limit=200`, null, { signal: s }),
    { pollMs: isTerminal ? 0 : 5000, deps: [sid, session?.status] },
  );
  const records = log.data?.items || [];
  // Project turn-log records into the TurnRow shape (model/tokens/dur/finish).
  const turns = React.useMemo(() => records
    .filter((e) => e.kind === "started" || e.kind === "completed" || e.kind === "failed")
    .map((e) => ({
      status: e.kind === "completed" ? "ended" : e.kind === "failed" ? "failed" : "running",
      started_at: e.ts,
      duration_ms: e.duration_ms,
      tokens_in: e.input_tokens,
      tokens_out: e.output_tokens,
      output: e.finish_reason || e.yield_kind || null,
      tool_calls: [],
    })), [records]);

  if (log.loading && !log.data) {
    return <div className="muted text-sm" style={{ padding: 12 }}>Loading turns…</div>;
  }
  if (turns.length === 0) {
    return (
      <div className="panel">
        <div className="panel-h"><Icon name="layers" size={13} /><span>Turn timeline</span></div>
        <div className="panel-body"><div className="muted text-sm" style={{ padding: 8 }}>No turns yet.</div></div>
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name="layers" size={13} /><span>Turn timeline</span>
        <span className="sub">· {turns.length} turn{turns.length === 1 ? "" : "s"}</span>
      </div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {turns.map((t, i) => <TurnRow key={i} turn={t} index={i} />)}
      </div>
    </div>
  );
}
window.SD_AgentTurnTimeline = SD_AgentTurnTimeline;
