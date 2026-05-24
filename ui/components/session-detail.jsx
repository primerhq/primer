/* global React, Icon, StatusPill, Btn, Modal, Banner, relativeTime, fmtDate */

const SESSION_TERMINAL = new Set(["ended", "completed", "failed", "cancelled"]);

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
  const { useResource, useMutation, useRouter, apiFetch } = window.matrixApi;
  const { params, navigate } = useRouter();
  const sid = sidProp || params.id;

  const [steer, setSteer] = React.useState("");
  const [showCancel, setShowCancel] = React.useState(false);
  const [queuedInstructions, setQueuedInstructions] = React.useState([]);
  const [turnsOpen, setTurnsOpen] = React.useState(true);
  const [errorOpen, setErrorOpen] = React.useState(true);
  const [metaOpen, setMetaOpen] = React.useState(false);

  // Top-level /v1/sessions/{id} is the authoritative path per app spec
  // §12 (T0399/T0555/T0611). Poll every 2s while non-terminal; pause
  // once terminal so we don't spam reads for unchanging rows. The
  // status check uses a ref so the pauseWhile closure stays stable.
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

  const turns = Array.isArray(session.turns) ? session.turns : [];
  const lastWorker = session.last_worker_id || session.worker_id;
  const boundAgent = session.binding?.agent_id || session.agent_id;
  const boundGraph = session.binding?.graph_id || session.graph_id;
  const lastError = session.last_error || session.error;
  const metadata = session.metadata || {};

  return (
    <div className="col">
      {isGraph && (
        <Banner
          kind="warning"
          icon="alert"
          title="Graph executor is unimplemented"
          detail="This session is bound to a graph. The graph executor currently raises NotImplementedError, so the session ends with `failed` on the first turn. Pinned in app spec §12."
        />
      )}

      {/* Yielding-tools surfaces. Each polls /ask_user/pending (404 = nothing). */}
      <AskUserPanel sid={sid} sessionStatus={session.status} pushToast={pushToast} />
      {wid && (
        <WatchFilesPanel sid={sid} wid={wid} session={session} pushToast={pushToast} />
      )}
      {wid && (
        <SleepPanel sid={sid} wid={wid} session={session} pushToast={pushToast} />
      )}

      <div className="session-detail-grid">
        {/* LEFT — primary */}
        <div className="col" style={{ gap: 14 }}>
          {/* Header card */}
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
                    {session.status === "running" && `turn ${session.turn_count ?? 0}${session.started_at ? ` · started ${relativeTime(_sdAgeSec(session.started_at))}` : ""}`}
                    {session.status === "paused" && `paused at turn ${session.turn_count ?? 0}`}
                    {session.status === "created" && "awaiting worker claim"}
                    {(session.status === "ended" || session.status === "completed") && `completed ${session.turn_count ?? 0} turn${(session.turn_count ?? 0) === 1 ? "" : "s"}`}
                    {session.status === "failed" && "failed during execution"}
                    {session.status === "cancelled" && "cancelled by operator"}
                  </span>
                </div>
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

          {/* Initial instructions */}
          {(session.initial_instructions || session.instructions) && (
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
          )}

          {/* Turns timeline */}
          <div className="panel">
            <div className="panel-h" onClick={() => setTurnsOpen(!turnsOpen)} style={{ cursor: "pointer" }}>
              <Icon name={turnsOpen ? "chevron-down" : "chevron-right"} size={12} className="muted" />
              <span>Turns timeline</span>
              <span className="sub">· {turns.length} turn{turns.length === 1 ? "" : "s"}</span>
              <div className="right">
                {session.status === "running" && (
                  <span className="text-sm mono" style={{ color: "var(--blue)" }}>● live</span>
                )}
              </div>
            </div>
            {turnsOpen && (
              <div className="panel-body">
                {turns.length === 0 ? (
                  <div className="muted text-sm" style={{ textAlign: "center", padding: 20 }}>
                    {session.status === "created"
                      ? "No turns yet — session is awaiting worker claim."
                      : "No turns yet on this session's row."}
                  </div>
                ) : (
                  <div className="turn-list">
                    {turns.map((t, i) => (
                      <TurnRow key={i} turn={t} index={i} />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Last error */}
          {lastError && (
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
                </div>
              )}
            </div>
          )}

          {/* Metadata */}
          {metadata && Object.keys(metadata).length > 0 && (
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
          )}
        </div>

        {/* RIGHT — controls + signals */}
        <div className="col" style={{ gap: 14 }}>
          {/* Signals */}
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
              <div style={{ borderTop: "1px solid var(--border)", margin: "4px -14px 0" }} />

              <div className="field-label mt-2" style={{ marginBottom: 4 }}>
                Steer instruction
                <span className="hint">does not gate on status — pinned spec §12</span>
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

          {/* References — cross-page anchors (U0105) */}
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
                <div className="ref-row">
                  <Icon name="graph" size={13} className="ico" />
                  <span className="label">Graph</span>
                  <span className="val"><a style={{ cursor: "pointer" }} onClick={() => navigate("/graphs/" + boundGraph)}>{boundGraph}</a></span>
                  <span className="pill pill-failed"><span className="dot"></span>executor missing</span>
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
        </div>
      </div>

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
// Yielding-tools UI surfaces
// =================================================================

// AskUserPanel — polls GET /v1/sessions/{sid}/ask_user/pending (200 =
// render; 404 = render nothing). Submit/Skip post to the real
// endpoints; 422/500 are surfaced INLINE via data-testid="ask-user-error"
// (U0051/U0060), success surfaces as a toast (U0049/U0050).
function AskUserPanel({ sid, sessionStatus, pushToast }) {
  const { useResource, apiFetch } = window.matrixApi;
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
  const { useMutation, apiFetch } = window.matrixApi;
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

window.AskUserPanel = AskUserPanel;
window.WatchFilesPanel = WatchFilesPanel;
window.SleepPanel = SleepPanel;
