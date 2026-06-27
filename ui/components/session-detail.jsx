/* global React, Icon, StatusPill, Btn, Modal, Banner, ApprovalBanner, MobileTabs, relativeTime, fmtDate */


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

function SessionDetail({ sid: sidProp, pushToast, onBack }) {
  const { useResource, useMutation, useRouter, useViewport, apiFetch } = window.primerApi;
  const { params, navigate, query, path } = useRouter();
  const { isMobile } = useViewport();
  const sid = sidProp || params.id;
  const activeTab = query.tab || "overview";
  const setTab = (id) => navigate(path, { ...query, tab: id });

  const [steer, setSteer] = React.useState("");
  const [showCancel, setShowCancel] = React.useState(false);
  const [showDelete, setShowDelete] = React.useState(false);
  const [queuedInstructions, setQueuedInstructions] = React.useState([]);
  const [errorOpen, setErrorOpen] = React.useState(true);
  const [metaOpen, setMetaOpen] = React.useState(false);

  // Poll every 2s while non-terminal; pause once terminal so we don't
  // spam reads for unchanging rows. The status check uses a ref so the
  // pauseWhile closure stays stable.
  const lastStatusRef = React.useRef(null);
  const detail = useResource(
    `session-detail:${sid}`,
    (signal) => apiFetch("GET", `/sessions/${encodeURIComponent(sid)}`, null, { signal }),
    {
      pollMs: 2000,
      pauseWhile: () => SESSION_TERMINAL.has(lastStatusRef.current),
      deps: [sid],
    }
  );

  const session = detail.data;
  React.useEffect(() => {
    lastStatusRef.current = session?.status || null;
  }, [session?.status]);
  const wid = session?.workspace_id;
  const isTerminal = session && SESSION_TERMINAL.has(session.status);
  const isGraph = (session?.binding?.kind || session?.binding_kind) === "graph";

  // Signal mutations. workspace-scoped endpoints per app spec §13.
  // All invalidate session-detail + sessions:list for fast feedback.
  const invalidates = [`session-detail:${sid}`, "sessions:list"];
  const pauseMut = useMutation(
    () => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/pause`),
    {
      invalidates,
      onSuccess: () => pushToast && pushToast({
        kind: "success",
        title: "Session paused",
        detail: "Worker will halt after current turn.",
      }),
      onError: _sdToastErr(pushToast, "Pause failed"),
    }
  );
  const resumeMut = useMutation(
    () => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/resume`),
    {
      invalidates,
      onSuccess: () => pushToast && pushToast({
        kind: "success",
        title: "Resume signal sent",
        detail: "Idempotent — 200 no-op if already running.",
      }),
      onError: _sdToastErr(pushToast, "Resume failed"),
    }
  );
  const cancelMut = useMutation(
    () => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/cancel`),
    {
      invalidates,
      onSuccess: () => pushToast && pushToast({
        kind: "warning",
        title: "Cancel signal sent",
        detail: "May take up to ~30s if the worker is mid-turn.",
      }),
      onError: _sdToastErr(pushToast, "Cancel failed"),
    }
  );
  const steerMut = useMutation(
    (instruction) => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/steer`, { instruction }),
    {
      invalidates,
      onSuccess: () => pushToast && pushToast({
        kind: "success",
        title: "Steer queued",
        detail: "Picked up at the next turn boundary.",
      }),
      onError: _sdToastErr(pushToast, "Steer failed"),
    }
  );
  const deleteMut = useMutation(
    ({ force = false } = {}) => apiFetch(
      "DELETE",
      `/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}${force ? "?force=true" : ""}`,
    ),
    {
      invalidates: ["sessions", "sessions:list", `session-detail:${sid}`],
      onSuccess: () => {
        setShowDelete(false);
        pushToast && pushToast({
          kind: "success",
          title: "Session deleted",
          detail: sid,
        });
        navigate("/sessions");
      },
      onError: (err) => {
        // 409 on RUNNING — offer the force path inline (the modal stays open).
        if (err && err.status === 409) {
          pushToast && pushToast({
            kind: "warning",
            title: "Session is running",
            detail: "Cancel it first, or use Force delete to evict an orphaned row.",
          });
        } else {
          _sdToastErr(pushToast, "Delete failed")(err);
        }
      },
    }
  );

  const onPause = () => { if (wid) pauseMut.mutate(); };
  const onResume = () => { if (wid) resumeMut.mutate(); };
  const onCancelConfirmed = () => {
    setShowCancel(false);
    if (wid) cancelMut.mutate();
  };
  const onSteer = () => {
    const text = steer.trim();
    if (!text || !wid) return;
    setQueuedInstructions((q) => [...q, { text, at: new Date() }]);
    setSteer("");
    steerMut.mutate(text);
  };

  // --- Render-state branches ---
  if (detail.loading && !session) {
    return (
      <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>
        Loading session {sid}…
      </div>
    );
  }
  if (detail.error && !session) {
    if (detail.error.status === 404) {
      return (
        <div className="panel">
          <div className="empty" style={{ padding: "40px 20px" }}>
            <div className="ico-wrap"><Icon name="x-circle" size={22} /></div>
            <div className="head">Session not found</div>
            <div className="sub">No row at <span className="mono">/v1/sessions/{sid}</span>. It may have been deleted, or the id is wrong.</div>
            <div className="actions">
              <Btn kind="primary" icon="chevron-left" onClick={onBack || (() => navigate("/sessions"))}>Back to list</Btn>
            </div>
          </div>
        </div>
      );
    }
    return (
      <Banner
        kind="error"
        title={detail.error.title || "Couldn't load session"}
        detail={detail.error.detail || detail.error.message}
        requestId={detail.error.requestId}
        actions={<Btn size="sm" icon="refresh" onClick={detail.refetch}>Retry</Btn>}
      />
    );
  }
  if (!session) return null;

  const lastWorker = session.last_worker_id || session.worker_id;
  const boundAgent = session.binding?.agent_id || session.agent_id;
  const boundGraph = session.binding?.graph_id || session.graph_id;
  const lastError = session.last_error || session.error;
  const metadata = session.metadata || {};

  // Lifted panel JSX — both desktop split-pane and MobileTabs render
  // the same panels, so we build them once and share between branches.
  const headerPanel = (
        <div className="panel">
            <div className="panel-body" style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 18, alignItems: "flex-start" }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
                  <span className="mono" style={{ fontSize: 17, fontWeight: 600 }}>{session.id}</span>
                  <button className="icon-btn" style={{ width: 24, height: 24 }} title="Copy id" onClick={() => navigator.clipboard && navigator.clipboard.writeText(session.id)}>
                    <Icon name="copy" size={11} />
                  </button>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
                  <StatusPill status={session.status} />
                  <span className="muted text-sm">
                    {session.status === "running" && `turn ${session.turn_no ?? session.turn_count ?? 0}${session.started_at ? ` · started ${relativeTime(_sdAgeSec(session.started_at))}` : ""}`}
                    {session.status === "paused" && `paused at turn ${session.turn_no ?? session.turn_count ?? 0}`}
                    {session.status === "created" && "awaiting worker claim"}
                    {(session.status === "ended" || session.status === "completed") && `completed ${session.turn_no ?? session.turn_count ?? 0} turn${(session.turn_no ?? session.turn_count ?? 0) === 1 ? "" : "s"}`}
                    {session.status === "failed" && "failed during execution"}
                    {session.status === "cancelled" && "cancelled by operator"}
                  </span>
                </div>
                {!isGraph && <SD_AgentStatusLine session={session} sid={sid} />}
                <dl className="kv">
                  <dt>bound</dt>
                  <dd>
                    {isGraph ? (
                      <>graph · <a style={{ color: "var(--violet)", cursor: "pointer" }} onClick={() => navigate("/graphs/" + boundGraph)}>{boundGraph}</a></>
                    ) : (
                      <>agent · <a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => navigate("/agents/" + boundAgent)}>{boundAgent || "—"}</a></>
                    )}
                  </dd>
                  <dt>workspace</dt>
                  <dd><a style={{ color: "var(--text)", cursor: "pointer" }} onClick={() => navigate("/workspaces/" + wid)}>{wid}</a></dd>
                  {session.created_at && (<>
                    <dt>created</dt>
                    <dd>{fmtDate(new Date(session.created_at))} <span className="muted">· {relativeTime(_sdAgeSec(session.created_at))}</span></dd>
                  </>)}
                  {session.started_at && (<>
                    <dt>started</dt>
                    <dd>{fmtDate(new Date(session.started_at))}</dd>
                  </>)}
                  {session.last_turn_at && (<>
                    <dt>last turn</dt>
                    <dd>{fmtDate(new Date(session.last_turn_at))} <span className="muted">· {relativeTime(_sdAgeSec(session.last_turn_at))}</span></dd>
                  </>)}
                  {session.attempt != null && (<>
                    <dt>attempt</dt>
                    <dd>{session.attempt}</dd>
                  </>)}
                  <dt>worker</dt>
                  <dd>{lastWorker ? <a style={{ color: "var(--text)", cursor: "pointer" }} onClick={() => navigate("/workers")}>{lastWorker}</a> : <span className="muted">—</span>}</dd>
                </dl>
              </div>
              <Btn
                size="sm"
                kind="ghost"
                icon="external"
                onClick={() => window.open("/v1/sessions/" + encodeURIComponent(session.id), "_blank", "noopener,noreferrer")}
              >View JSON</Btn>
            </div>
          </div>
  );

  const instructionsPanel = (session.initial_instructions || session.instructions) ? (
            <div className="panel">
              <div className="panel-h">
                <span>Initial instructions</span>
                <div className="right">
                  <span className="muted text-sm">{(session.initial_instructions || session.instructions || "").length} chars</span>
                </div>
              </div>
              <div className="panel-body" style={{ padding: 0 }}>
                <div className="code-block" style={{ border: "none", borderRadius: 0, background: "transparent" }}>
                  {session.initial_instructions || session.instructions}
                </div>
              </div>
            </div>
  ) : null;

  const liveStreamPanel = wid ? (
            <SessionLiveStream sid={sid} wid={wid} session={session} pushToast={pushToast} />
  ) : null;

  const lastErrorPanel = lastError ? (
            <div className="panel" style={{ borderColor: "oklch(0.7 0.2 25 / 0.4)" }}>
              <div className="panel-h" onClick={() => setErrorOpen(!errorOpen)} style={{ cursor: "pointer", background: "var(--red-dim)" }}>
                <Icon name={errorOpen ? "chevron-down" : "chevron-right"} size={12} style={{ color: "var(--red)" }} />
                <Icon name="x-circle" size={13} style={{ color: "var(--red)" }} />
                <span style={{ color: "var(--red)" }}>Last error</span>
                {lastError.type && <span className="mono sub">· {lastError.type}</span>}
                <div className="right">
                  {lastError.extensions?.request_id && (
                    <Btn
                      size="sm"
                      kind="ghost"
                      icon="copy"
                      onClick={(e) => { e.stopPropagation(); navigator.clipboard && navigator.clipboard.writeText(lastError.extensions.request_id); }}
                    >Copy request-id</Btn>
                  )}
                </div>
              </div>
              {errorOpen && (
                <div className="panel-body">
                  {lastError.title && <div style={{ fontWeight: 600, marginBottom: 4 }}>{lastError.title}</div>}
                  {lastError.detail && <div className="muted text-sm mb-3">{lastError.detail}</div>}
                  <div className="code-block">
                    {JSON.stringify(lastError, null, 2)}
                  </div>
                  {session?.ended_reason === "workspace_lost" && session?.workspace_id && (
                    <WorkspaceFailureChip workspaceId={session.workspace_id} />
                  )}
                </div>
              )}
            </div>
  ) : (
    session?.ended_reason === "workspace_lost" && session?.workspace_id ? (
      <div className="panel" style={{ borderColor: "oklch(0.7 0.2 25 / 0.4)" }}>
        <div className="panel-h" style={{ background: "var(--red-dim)" }}>
          <Icon name="x-circle" size={13} style={{ color: "var(--red)" }} />
          <span style={{ color: "var(--red)" }}>Workspace lost</span>
        </div>
        <div className="panel-body">
          <WorkspaceFailureChip workspaceId={session.workspace_id} />
        </div>
      </div>
    ) : null
  );

  const metadataPanel = (metadata && Object.keys(metadata).length > 0) ? (
            <div className="panel">
              <div className="panel-h" onClick={() => setMetaOpen(!metaOpen)} style={{ cursor: "pointer" }}>
                <Icon name={metaOpen ? "chevron-down" : "chevron-right"} size={12} className="muted" />
                <span>Metadata</span>
                <span className="sub">· {Object.keys(metadata).length} key{Object.keys(metadata).length === 1 ? "" : "s"}</span>
              </div>
              {metaOpen && (
                <div className="panel-body">
                  <dl className="kv" style={{ gridTemplateColumns: "180px 1fr" }}>
                    {Object.entries(metadata).map(([k, v]) => (
                      <React.Fragment key={k}>
                        <dt>{k}</dt>
                        <dd className="mono">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
                      </React.Fragment>
                    ))}
                  </dl>
                </div>
              )}
            </div>
  ) : null;

  const signalsPanel = (
          <div className="panel">
            <div className="panel-h">
              <Icon name="zap" size={13} style={{ color: "var(--accent)" }} />
              <span>Live signals</span>
            </div>
            <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <Btn
                disabled={session.status !== "running" || pauseMut.loading}
                icon="pause"
                onClick={onPause}
                title={session.status !== "running" ? "Enabled only when status = running" : ""}
              >Pause</Btn>
              <Btn
                icon="play"
                onClick={onResume}
                disabled={isTerminal || resumeMut.loading}
                title="Idempotent — returns 200 no-op if already running (per app spec §13)"
              >Resume</Btn>
              <Btn
                kind="danger"
                disabled={isTerminal || cancelMut.loading}
                icon="stop"
                onClick={() => setShowCancel(true)}
              >Cancel</Btn>
              <Btn
                kind="danger"
                disabled={deleteMut.loading}
                icon="trash"
                onClick={() => setShowDelete(true)}
                data-testid="session-delete-btn"
              >Delete</Btn>
              <div style={{ borderTop: "1px solid var(--border)", margin: "4px -14px 0" }} />

              <div className="field-label mt-2" style={{ marginBottom: 4 }}>
                Steer instruction
              </div>
              <textarea
                className="textarea mono"
                placeholder="Drop a hint or new directive for the next turn…"
                value={steer}
                onChange={(e) => setSteer(e.target.value)}
                rows={3}
                style={{ fontSize: 12 }}
              />
              <Btn
                kind="primary"
                icon="send"
                onClick={onSteer}
                disabled={!steer.trim() || steerMut.loading}
              >Queue steer</Btn>

              {queuedInstructions.length > 0 && (
                <div style={{ marginTop: 8 }}>
                  <div className="field-label" style={{ marginBottom: 4 }}>Queued this session ({queuedInstructions.length})</div>
                  {queuedInstructions.map((q, i) => (
                    <div key={i} className="tool-call" style={{ flexDirection: "column", alignItems: "flex-start" }}>
                      <div style={{ color: "var(--text)", fontFamily: "inherit" }}>{q.text}</div>
                      <div className="muted text-sm">queued {relativeTime(_sdAgeSec(q.at))}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
  );

  const referencesPanel = (
          <div className="panel">
            <div className="panel-h">
              <Icon name="fork" size={13} />
              <span>References</span>
            </div>
            <div className="panel-body" style={{ padding: "4px 14px" }}>
              {!isGraph && boundAgent && (
                <div className="ref-row">
                  <Icon name="agent" size={13} className="ico" />
                  <span className="label">Agent</span>
                  <span className="val"><a style={{ cursor: "pointer" }} onClick={() => navigate("/agents/" + boundAgent)}>{boundAgent}</a></span>
                </div>
              )}
              {isGraph && boundGraph && (
                <div className="ref-row" style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <Icon name="graph" size={13} className="ico" />
                    <span className="label">Graph</span>
                    <span className="val"><a style={{ cursor: "pointer" }} onClick={() => navigate("/graphs/" + boundGraph)}>{boundGraph}</a></span>
                  </div>
                  <SD_GraphHealthPanel gid={boundGraph} />
                </div>
              )}
              {wid && (
                <div className="ref-row">
                  <Icon name="box" size={13} className="ico" />
                  <span className="label">Workspace</span>
                  <span className="val"><a style={{ cursor: "pointer" }} onClick={() => navigate("/workspaces/" + wid)}>{wid}</a></span>
                </div>
              )}
              {lastWorker && (
                <div className="ref-row">
                  <Icon name="worker" size={13} className="ico" />
                  <span className="label">Worker</span>
                  <span className="val"><a style={{ cursor: "pointer" }} onClick={() => navigate("/workers")}>{lastWorker}</a></span>
                </div>
              )}
            </div>
          </div>
  );


  // Tab content for MobileTabs. Each tab content is a column of the
  // already-built panel JSX consts above — the desktop split-pane
  // renders the same JSX so we never duplicate panel bodies.
  //  • overview  → header + references + stale-cache notice
  //  • messages  → live WS stream (the chat-like timeline)
  //  • state     → signals, initial instructions, turns, last error, metadata
  //  • files     → parked yields (WatchFiles / Sleep) when present
  const filesParked = (session?.parked_status === "parked" || session?.parked_status === "waiting")
    && (session?.parked_state?.yielded?.tool_name === "watch_files"
        || session?.parked_state?.yielded?.tool_name === "sleep"
        || session?.parked_state?.tool_name === "watch_files"
        || session?.parked_state?.tool_name === "sleep");
  const filesPanel = wid && filesParked ? (
    <div className="col" style={{ gap: 14 }}>
      <WatchFilesPanel sid={sid} wid={wid} session={session} pushToast={pushToast} />
      <SleepPanel sid={sid} wid={wid} session={session} pushToast={pushToast} />
    </div>
  ) : (
    <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
      No parked file or sleep yields. Watching surfaces here when active.
    </div>
  );

  const tabs = [
    {
      id: "overview",
      label: "Overview",
      content: (
        <div className="col" style={{ gap: 14, padding: 12 }}>
          {headerPanel}
          {referencesPanel}
        </div>
      ),
    },
    {
      id: "messages",
      label: "Messages",
      content: (
        <div className="col" style={{ gap: 14, padding: 12 }}>
          {isGraph && wid ? (
            <SD_GraphRunView gid={boundGraph} rid={sid} wid={wid} session={session} pushToast={pushToast} />
          ) : liveStreamPanel || (
            <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
              Live stream unavailable — session has no workspace_id.
            </div>
          )}
        </div>
      ),
    },
    {
      id: "state",
      label: "State",
      content: (
        <div className="col" style={{ gap: 14, padding: 12 }}>
          {signalsPanel}
          {instructionsPanel}
          {!isGraph && <SD_AgentTurnTimeline sid={sid} session={session} />}
          {lastErrorPanel}
          {metadataPanel}
        </div>
      ),
    },
    {
      id: "files",
      label: "Files",
      content: filesPanel,
    },
    {
      id: "turnlog",
      label: "Turn log",
      content: (
        <TurnLogTab
          sessionId={sid}
          sessionStatus={session?.status}
          binding={session?.binding}
        />
      ),
    },
  ];

  return (
    <div className="col">
      {/* Yielding-tools surfaces. Each polls /ask_user/pending (404 = nothing). */}
      <AskUserPanel sid={sid} sessionStatus={session.status} pushToast={pushToast} />
      <ApprovalBannerPanel sid={sid} sessionStatus={session.status} pushToast={pushToast} />
      {isGraph && boundGraph && (
        <SD_CannotRunBanner gid={boundGraph} session={session} />
      )}
      {!isMobile && wid && (
        <WatchFilesPanel sid={sid} wid={wid} session={session} pushToast={pushToast} />
      )}
      {!isMobile && wid && (
        <SleepPanel sid={sid} wid={wid} session={session} pushToast={pushToast} />
      )}

      {isMobile ? (
        <MobileTabs tabs={tabs} active={activeTab} onSelect={setTab} />
      ) : (
        <div className="session-detail-grid">
          {/* LEFT — primary */}
          <div className="col" style={{ gap: 14 }}>
            {headerPanel}
            {instructionsPanel}
            {!isGraph && <SD_AgentTurnTimeline sid={sid} session={session} />}
            {isGraph && wid
              ? <SD_GraphRunView gid={boundGraph} rid={sid} wid={wid} session={session} pushToast={pushToast} />
              : liveStreamPanel}
            {lastErrorPanel}
            {metadataPanel}
          </div>

          {/* RIGHT — controls + signals */}
          <div className="col" style={{ gap: 14 }}>
            {signalsPanel}
            {referencesPanel}
          </div>
        </div>
      )}

      {showCancel && (
        <Modal
          title="Cancel session?"
          danger
          onClose={() => setShowCancel(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setShowCancel(false)}>Keep running</Btn>
              <Btn kind="danger" icon="stop" onClick={onCancelConfirmed}>Cancel session</Btn>
            </>
          }
        >
          Sending a cancel signal to <strong className="mono" style={{ fontFamily: "inherit" }}>{session.id}</strong>.
          <ul>
            <li>The worker will finish or abandon the current turn — this may take up to ~30s.</li>
            <li>Any queued steer instructions will be discarded.</li>
            <li>The workspace and its <span className="mono" style={{ fontSize: 11 }}>.state</span> are not affected.</li>
          </ul>
        </Modal>
      )}
      {showDelete && (
        <Modal
          title="Delete session?"
          danger
          onClose={() => setShowDelete(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setShowDelete(false)}>Keep</Btn>
              {session.status === "running" ? (
                <Btn
                  kind="danger"
                  icon="trash"
                  disabled={deleteMut.loading}
                  onClick={() => deleteMut.mutate({ force: true })}
                  data-testid="session-force-delete-confirm"
                >Force delete</Btn>
              ) : (
                <Btn
                  kind="danger"
                  icon="trash"
                  disabled={deleteMut.loading}
                  onClick={() => deleteMut.mutate({})}
                  data-testid="session-delete-confirm"
                >Delete</Btn>
              )}
            </>
          }
        >
          Removing <strong className="mono" style={{ fontFamily: "inherit" }}>{session.id}</strong>.
          {session.status === "running" ? (
            <ul>
              <li><strong>Session is RUNNING.</strong> Force delete evicts the row without waiting for the worker — use only if the worker is stuck/orphaned.</li>
              <li>Otherwise cancel the session first, then delete it normally.</li>
              <li>The workspace and its <span className="mono" style={{ fontSize: 11 }}>.state</span> are not affected.</li>
            </ul>
          ) : (
            <ul>
              <li>The server auto-cancels CREATED / WAITING / PAUSED sessions before deletion.</li>
              <li>Any queued steer instructions are discarded.</li>
              <li>The workspace and its <span className="mono" style={{ fontSize: 11 }}>.state</span> are not affected.</li>
            </ul>
          )}
        </Modal>
      )}
    </div>
  );
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

window.SessionDetail = SessionDetail;

// =================================================================
// SessionLiveStream — WS live-watch panel (Task 14)
// =================================================================
//
// Subscribes to WS /v1/workspaces/{wid}/sessions/{sid}/ws?cursor=0.
// Dispatches frames into a local messages list and renders them like
// the ChatDetail conversation view from chats.jsx. Mirrored approach:
//  • assistant_token rows are coalesced into one bubble
//  • tool_call / tool_result render as expandable cards
//  • done / cancelled / yielded / resumed render as event markers
//  • error renders as an inline error banner
//  • user_input renders as a user bubble
//  • "Thinking…" appears when turn_status === "running" | "claimable"
//  • "Interrupt" button sends {"kind":"interrupt"} down the socket
//
// The panel only mounts if wid is known (session row has workspace_id).


function SessionLiveStream({ sid, wid, session, pushToast }) {
  const [messages, setMessages] = React.useState([]);
  const [wsState, setWsState] = React.useState("connecting");
  // Token-usage snapshot for the read-only header TokenMeter. Hydrated
  // from any `"usage"` WS envelope the worker emits; if the session
  // WS never carries one, we fall back below to the most recent turn's
  // tokens_in so the meter still surfaces something meaningful.
  const [usage, setUsage] = React.useState({ input_tokens: 0, output_tokens: 0, context_length: 0 });
  const wsRef = React.useRef(null);
  const scrollRef = React.useRef(null);

  const isRunning = session?.turn_status === "running" || session?.turn_status === "claimable";

  // WS lifecycle — reconnects on wid/sid change.
  // Initial connection uses cursor=0 so the server replays the full
  // session history then tails live. De-duplication handles overlap.
  //
  // Reconnect: an unexpected close triggers exponential-backoff
  // reconnect (1s -> 2s -> 4s ... 30s cap). Reconnections resume from
  // the last received seq (latestSeq) so no frames are missed or
  // replayed in full. Terminal close code 4404 does not reconnect.
  React.useEffect(() => {
    if (!wid || !sid) return;
    let intentional = false;
    let backoffMs = 1000;
    const MAX_BACKOFF_MS = 30000;
    let reconnectTimer = null;
    // Track the highest seq seen. Starts at 0 (full replay on first
    // connect); updated on each frame so reconnects resume cleanly.
    let latestSeq = 0;

    function connect() {
      if (intentional) return;
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${window.location.host}/v1/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/ws?cursor=${latestSeq}`;
      let ws;
      try {
        ws = new WebSocket(url);
      } catch {
        setWsState("closed");
        return;
      }
      wsRef.current = ws;
      setWsState("connecting");

      ws.onopen = () => {
        setWsState("open");
        backoffMs = 1000; // reset on successful connect
      };

      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (!msg || typeof msg !== "object") return;

        // Protocol-level error (no seq) - toast and bail.
        if (msg.kind === "error" && typeof msg.seq !== "number") {
          if (typeof pushToast === "function") {
            pushToast({ kind: "error", title: msg.code || "Session WS error", detail: msg.message || "" });
          }
          return;
        }
        if (msg.kind === "pong") return;

        // Token-usage envelope (no seq). Drives the read-only header
        // TokenMeter - the session WS surface mirrors the chats WS for
        // this shape (`input_tokens` / `context_length`).
        if (msg.kind === "usage" && typeof msg.seq !== "number") {
          setUsage({
            input_tokens: Number(msg.input_tokens) || 0,
            output_tokens: Number(msg.output_tokens) || 0,
            context_length: Number(msg.context_length) || 0,
          });
          return;
        }

        // Persisted frame - deduplicate and append.
        if (typeof msg.seq === "number") {
          if (msg.seq > latestSeq) latestSeq = msg.seq;
          // Flatten payload into top-level (mirrors chats.jsx approach).
          const payload = msg.payload && typeof msg.payload === "object" ? msg.payload : {};
          const frame = { ...payload, ...msg };
          setMessages((prev) => {
            if (prev.some((p) => p.seq === frame.seq)) return prev;
            return [...prev, frame];
          });
        }
      };

      ws.onclose = (ev) => {
        wsRef.current = null;
        setWsState("closed");
        if (ev.code === 4404) {
          if (typeof pushToast === "function") {
            pushToast({ kind: "error", title: "Session not found via WS", detail: ev.reason || sid });
          }
          return;
        }
        // Unexpected close - reconnect with exponential backoff.
        if (!intentional) {
          reconnectTimer = setTimeout(() => {
            backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
            connect();
          }, backoffMs);
        }
      };

      ws.onerror = () => { /* onclose handles user-facing messaging and reconnect */ };
    }

    connect();

    return () => {
      intentional = true;
      if (reconnectTimer != null) clearTimeout(reconnectTimer);
      try { wsRef.current && wsRef.current.close(); } catch { /* no-op */ }
      wsRef.current = null;
    };
  }, [wid, sid]); // eslint-disable-line react-hooks/exhaustive-deps

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

  const sendInterrupt = () => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== 1) {
      if (typeof pushToast === "function") {
        pushToast({ kind: "error", title: "Not connected", detail: "WebSocket is not open" });
      }
      return;
    }
    ws.send(JSON.stringify({ kind: "interrupt" }));
    if (typeof pushToast === "function") {
      pushToast({ kind: "warning", title: "Interrupt sent", detail: "Session will cancel after current step." });
    }
  };

  const wsBadge = wsState === "open"
    ? <span className="pill pill-running" title="WebSocket open"><span className="dot"></span>live</span>
    : wsState === "connecting"
      ? <span className="pill pill-paused" title="WebSocket connecting"><span className="dot"></span>connecting</span>
      : <span className="pill pill-ended" title="WebSocket closed"><span className="dot"></span>offline</span>;

  const coalesced = _SLS_coalesceMessages(messages);
  const isTerminalSession = session && SESSION_TERMINAL.has(session.status);

  // Fallback for the read-only TokenMeter when no `"usage"` envelope
  // has landed yet. Use the most recent turn's recorded tokens_in so
  // we surface something on first paint; the WS envelope (if/when it
  // arrives) takes precedence.
  const fallbackUsage = React.useMemo(() => {
    const turns = Array.isArray(session?.turns) ? session.turns : [];
    const last = turns.length > 0 ? turns[turns.length - 1] : null;
    return {
      input_tokens: Number(last?.tokens_in) || 0,
      context_length: Number(session?.context_length) || 0,
    };
  }, [session]);
  const meterInput = usage.input_tokens || fallbackUsage.input_tokens;
  const meterContext = usage.context_length || fallbackUsage.context_length;

  return (
    <div className="panel" style={{ display: "flex", flexDirection: "column" }}>
      <div className="panel-h">
        <Icon name="zap" size={13} style={{ color: "var(--blue)" }} />
        <span>Live stream</span>
        <span className="sub">· {messages.length} frame{messages.length === 1 ? "" : "s"}</span>
        <div className="right" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {/* Read-only TokenMeter — workspace sessions surface context
              pressure but don't expose an operator-triggered compact
              action (those flow through the chat surface). */}
          <window.TokenMeter
            inputTokens={meterInput}
            contextLength={meterContext}
            onCompact={null}
          />
          {wsBadge}
          {!isTerminalSession && (
            <Btn
              size="sm"
              kind="danger"
              icon="stop"
              disabled={wsState !== "open"}
              onClick={sendInterrupt}
              title="Send interrupt frame to cancel the running turn"
            >Interrupt</Btn>
          )}
        </div>
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        style={{ flex: 1, overflow: "auto", padding: "14px 18px", minHeight: 120, maxHeight: 480 }}
      >
        {coalesced.length === 0 && (
          <div className="muted text-sm" style={{ textAlign: "center", padding: 20 }}>
            {wsState === "connecting"
              ? "Connecting to session stream…"
              : wsState === "closed"
                ? "Stream offline. No frames received or connection dropped."
                : "No frames yet — session has not started a turn."}
          </div>
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

// Overlays per-node status tint on top of the shared GR_Canvas without
// editing GR_Canvas: we render GR_Canvas read-only and absolutely
// position a tint ring per node using GR_NODE_SIZE for geometry.
function SD_StatusCanvas({ graph, statusByNode, selectedNodeId, onSelectNode }) {
  const draft = React.useMemo(() => {
    const base = { ...graph, nodes: (graph.nodes || []).map((n) => ({ ...n })), edges: (graph.edges || []).map((e) => ({ ...e })) };
    if (window.primerVendor && window.primerVendor.autoLayout) {
      return window.primerVendor.autoLayout(base);
    }
    return base;
  }, [graph]);

  return (
    <div style={{ position: "relative" }}>
      <window.GR_Canvas
        draft={draft}
        selectedNodeId={selectedNodeId}
        selectedEdgeId={null}
        addEdgeMode={null}
        onNodeClick={(id) => onSelectNode(id)}
        onEdgeClick={() => {}}
        onNodeDoubleClick={() => {}}
        onNodeMouseDown={() => {}}
        onBackgroundClick={() => onSelectNode(null)}
      />
      {/* Status tint rings positioned over each node. */}
      <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
        {(draft.nodes || []).map((n) => {
          const st = statusByNode[n.id] || "pending";
          const tint = SD_RUN_STATE_TINT[st] || SD_RUN_STATE_TINT.pending;
          const sz = window.GR_NODE_SIZE[n.kind] || window.GR_NODE_SIZE.agent;
          return (
            <div
              key={n.id}
              data-testid={`run-node-${n.id}`}
              data-status={st}
              style={{
                position: "absolute",
                left: (n.x || 0) - 2,
                top: (n.y || 0) - 2,
                width: sz.w + 4,
                height: sz.h + 4,
                borderRadius: n.kind === "begin" || n.kind === "end" ? "50%" : 10,
                border: `2px solid ${tint.border}`,
                boxShadow: tint.glow || undefined,
                animation: st === "running" ? "pulse 1.6s ease-in-out infinite" : undefined,
              }}
            />
          );
        })}
      </div>
    </div>
  );
}

function SD_GraphRunView({ gid, rid, wid, session, pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const isTerminal = session && window.SESSION_TERMINAL.has(session.status);
  const [selectedNodeId, setSelectedNodeId] = React.useState(null);

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

  // Superstep WS events trigger an immediate refetch so node transitions
  // feel live without a tight poll. We subscribe read-only to the same
  // session WS the live stream uses and refetch on superstep frames.
  React.useEffect(() => {
    if (!wid || !rid || isTerminal) return undefined;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/v1/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(rid)}/ws?cursor=0`;
    let ws;
    try { ws = new WebSocket(url); } catch { return undefined; }
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      const kind = msg && (msg.kind || (msg.payload && msg.payload.kind));
      if (kind === "superstep_started" || kind === "superstep_ended" || kind === "done" || kind === "error") {
        states.refetch();
      }
    };
    return () => { try { ws.close(); } catch { /* no-op */ } };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wid, rid, isTerminal]);

  const items = states.data?.items || [];
  const statusByNode = React.useMemo(() => {
    const out = {};
    for (const it of items) out[it.node_id] = it.status;
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
        <div className="right">
          <span className={`pill pill-${overall === "ended" ? "ended" : overall === "failed" ? "failed" : overall === "running" ? "running" : "paused"}`}>
            <span className="dot"></span>{overall}
          </span>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 360px" }}>
        <SD_StatusCanvas
          graph={graph.data}
          statusByNode={statusByNode}
          selectedNodeId={selectedNodeId}
          onSelectNode={setSelectedNodeId}
        />
        <SD_NodeInspector
          gid={gid}
          rid={rid}
          wid={wid}
          session={session}
          node={selectedItem}
          graph={graph.data}
          pushToast={pushToast}
        />
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

function SD_NodeInspector({ gid, rid, wid, session, node, graph, pushToast }) {
  const { useRouter } = window.primerApi;
  const { navigate } = useRouter();
  const nodeId = node && node.node_id;

  // Node-attributed output read-on-completion: the assistant stream is
  // read from the session WS frames carrying this node_id (graph failures
  // + End records already do); the per-node turn-log below is the audit
  // trail. Hooks run unconditionally (before the no-selection early
  // return) so the hook order is stable across node selection. v1 shows
  // the turn-log + node-attributed session frames; token-live per-node
  // output is the documented fast-follow (spec §8).
  const [frames, setFrames] = React.useState([]);
  React.useEffect(() => {
    setFrames([]);
    if (!wid || !rid || !nodeId) return undefined;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/v1/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(rid)}/ws?cursor=0`;
    let ws;
    try { ws = new WebSocket(url); } catch { return undefined; }
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (typeof msg.seq !== "number") return;
      const payload = msg.payload && typeof msg.payload === "object" ? msg.payload : {};
      const frame = { ...payload, ...msg };
      const fnode = frame.node_id || frame.end_node_id;
      if (fnode !== nodeId) return;
      setFrames((prev) => prev.some((p) => p.seq === frame.seq) ? prev : [...prev, frame]);
    };
    return () => { try { ws.close(); } catch { /* no-op */ } };
  }, [wid, rid, nodeId]);

  const coalesced = window._SLS_coalesceMessages(frames);

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
        {/* Per-node turn-log below the stream. */}
        <div style={{ borderTop: "1px solid var(--border)" }}>
          <div className="muted text-sm" style={{ padding: "8px 12px 0", textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 10.5 }}>
            Turn log
          </div>
          <SD_NodeTurnLog gid={gid} rid={rid} nodeId={node.node_id} nodeStatus={node.status} />
        </div>
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
function AskUserPanel({ sid, sessionStatus, pushToast }) {
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
function ApprovalBannerPanel({ sid, sessionStatus, pushToast }) {
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
    <ApprovalBanner data={pending.data} scope="sessions" id={sid} pushToast={pushToast} />
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
