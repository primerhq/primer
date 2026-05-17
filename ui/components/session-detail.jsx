/* global React, Icon, StatusPill, Btn, Modal, Banner, relativeTime, fmtDate */

const { apiFetch, useResource, useMutation, useRouter, useToast } = window.matrixApi;

const TERMINAL_STATUSES = new Set(["ended", "failed", "cancelled", "completed"]);

function _ageSec(iso) {
  return iso ? (Date.now() - new Date(iso).getTime()) / 1000 : null;
}

function SessionDetail() {
  const { params, navigate } = useRouter();
  const { push: pushToast } = useToast();
  const sid = params.id;

  // Top-level /v1/sessions/{id} is the authoritative path per app spec
  // §12 (T0399/T0555/T0611). The nested workspace path drifts after
  // signals; we never read from it. Polling at 2s while non-terminal,
  // no polling once terminal (data is final).
  const session = useResource(
    "session-detail:" + sid,
    (signal) => apiFetch("GET", "/sessions/" + encodeURIComponent(sid), null, { signal }),
    {
      // Same circularity problem as sessions-list.jsx; hardcode 2000ms
      // until we have a non-self-referential way to read the status.
      // After terminal, the polling is wasted but the requests are
      // small. Optimisation can come later.
      pollMs: 2000,
      deps: [sid],
    }
  );

  // Steer textarea state lives in this component, not the queue model.
  const [steer, setSteer] = React.useState("");
  const [showCancel, setShowCancel] = React.useState(false);
  const [queuedInstructions, setQueuedInstructions] = React.useState([]);
  const [turnsOpen, setTurnsOpen] = React.useState(true);
  const [errorOpen, setErrorOpen] = React.useState(true);
  const [metaOpen, setMetaOpen] = React.useState(false);

  const s = session.data;
  const wid = s?.workspace_id;
  const isTerminal = s && TERMINAL_STATUSES.has(s.status);
  const isGraph = s?.binding?.kind === "graph";

  // Signal mutations. All invalidate the same session-detail cacheKey
  // so the row refetches immediately (don't wait for the next poll
  // tick). Plus invalidate the sessions list caches so the operator
  // navigating back sees the new status.
  const invalidates = ["session-detail:" + sid, "/sessions?limit=200"];
  const pauseMut = useMutation(
    () => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/pause`),
    { invalidates, onError: _toastErr(pushToast, "Pause failed") }
  );
  const resumeMut = useMutation(
    () => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/resume`),
    { invalidates, onError: _toastErr(pushToast, "Resume failed") }
  );
  const cancelMut = useMutation(
    () => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/cancel`),
    { invalidates, onError: _toastErr(pushToast, "Cancel failed") }
  );
  const steerMut = useMutation(
    (instruction) => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/sessions/${encodeURIComponent(sid)}/steer`, { instruction }),
    { invalidates, onError: _toastErr(pushToast, "Steer failed") }
  );

  const onPause = async () => {
    try { await pauseMut.mutate(); pushToast({ kind: "success", title: "Session paused", detail: "Worker will halt after current turn." }); }
    catch (_e) { /* error toast pushed by onError */ }
  };
  const onResume = async () => {
    try {
      await resumeMut.mutate();
      pushToast({ kind: "info", title: "Resume signal sent", detail: "Resume is idempotent — 200 no-op if already running." });
    } catch (_e) {}
  };
  const onCancel = async () => {
    setShowCancel(false);
    try {
      await cancelMut.mutate();
      pushToast({ kind: "warning", title: "Cancel signal sent", detail: "May take up to ~30s if the worker is mid-turn." });
    } catch (_e) {}
  };
  const onSteer = async () => {
    if (!steer.trim()) return;
    const text = steer;
    setSteer("");
    setQueuedInstructions((q) => [...q, { text, at: new Date() }]);
    try { await steerMut.mutate(text); pushToast({ kind: "success", title: "Steer queued", detail: "Picked up at the next turn boundary." }); }
    catch (_e) { /* roll back the optimistic queue addition? for v1 leave it for audit */ }
  };

  // --- Render-state branches ---
  if (session.loading && !s) {
    return (
      <div className="col" style={{ gap: 14 }}>
        <SessionDetailHeader sid={sid} session={null} navigate={navigate} />
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading session {sid}…</div>
      </div>
    );
  }
  if (session.error && !s) {
    if (session.error.status === 404) {
      return (
        <div className="col" style={{ gap: 14 }}>
          <SessionDetailHeader sid={sid} session={null} navigate={navigate} />
          <div className="panel">
            <div className="empty" style={{ padding: "40px 20px" }}>
              <div className="ico-wrap"><Icon name="x-circle" size={22} /></div>
              <div className="head">Session not found</div>
              <div className="sub">No row at <span className="mono">/v1/sessions/{sid}</span>. It may have been deleted, or the id is wrong.</div>
              <div className="actions"><Btn kind="primary" icon="chevron-left" onClick={() => navigate("/sessions")}>Back to list</Btn></div>
            </div>
          </div>
        </div>
      );
    }
    return (
      <div className="col" style={{ gap: 14 }}>
        <SessionDetailHeader sid={sid} session={null} navigate={navigate} />
        <Banner
          kind="error"
          title={session.error.title || "Couldn't load session"}
          detail={session.error.detail || session.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={session.refetch}>Retry</Btn>}
        />
      </div>
    );
  }
  if (!s) return null;

  const turns = Array.isArray(s.turns) ? s.turns : [];

  return (
    <div className="col" style={{ gap: 14 }}>
      <SessionDetailHeader sid={sid} session={s} navigate={navigate} />

      {isGraph && (
        <Banner
          kind="warning"
          icon="alert"
          title="Graph executor is unimplemented"
          detail="This session is bound to a graph. The graph executor currently raises NotImplementedError, so the session ends with `failed` on the first turn. Pinned in app spec §12 (T0156)."
        />
      )}

      <div className="session-detail-grid">
        {/* LEFT — primary */}
        <div className="col" style={{ gap: 14 }}>
          {/* Header card */}
          <div className="panel">
            <div className="panel-body" style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 18, alignItems: "flex-start" }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
                  <span className="mono" style={{ fontSize: 17, fontWeight: 600 }}>{s.id}</span>
                  <button
                    className="icon-btn"
                    style={{ width: 24, height: 24 }}
                    title="Copy id"
                    onClick={() => navigator.clipboard && navigator.clipboard.writeText(s.id)}
                  ><Icon name="copy" size={11} /></button>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
                  <StatusPill status={s.status} />
                  <span className="muted text-sm">
                    {s.status === "running" && `turn ${s.turn_count ?? 0}${s.started_at ? ` · started ${relativeTime(_ageSec(s.started_at))}` : ""}`}
                    {s.status === "paused" && `paused at turn ${s.turn_count ?? 0}`}
                    {s.status === "created" && "awaiting worker claim"}
                    {(s.status === "ended" || s.status === "completed") && `completed ${s.turn_count ?? 0} turn${(s.turn_count ?? 0) === 1 ? "" : "s"}`}
                    {s.status === "failed" && "failed during execution"}
                    {s.status === "cancelled" && "cancelled by operator"}
                  </span>
                </div>
                <dl className="kv">
                  <dt>bound</dt>
                  <dd>
                    {isGraph
                      ? <>graph · <a style={{ color: "var(--violet)" }} onClick={() => navigate("/graphs/" + s.binding.graph_id)}>{s.binding.graph_id}</a></>
                      : <>agent · <a style={{ color: "var(--accent)" }} onClick={() => navigate("/agents/" + s.binding.agent_id)}>{s.binding?.agent_id || "—"}</a></>
                    }
                  </dd>
                  <dt>workspace</dt>
                  <dd>
                    <a style={{ color: "var(--text)", cursor: "pointer" }} onClick={() => navigate("/workspaces/" + wid)}>{wid}</a>
                  </dd>
                  {s.created_at && (<>
                    <dt>created</dt>
                    <dd>{fmtDate(new Date(s.created_at))} <span className="muted">· {relativeTime(_ageSec(s.created_at))}</span></dd>
                  </>)}
                  {s.started_at && (<>
                    <dt>started</dt>
                    <dd>{fmtDate(new Date(s.started_at))}</dd>
                  </>)}
                  {s.last_turn_at && (<>
                    <dt>last turn</dt>
                    <dd>{fmtDate(new Date(s.last_turn_at))} <span className="muted">· {relativeTime(_ageSec(s.last_turn_at))}</span></dd>
                  </>)}
                  {s.attempt != null && (<>
                    <dt>attempt</dt>
                    <dd>{s.attempt}</dd>
                  </>)}
                  <dt>worker</dt>
                  <dd>
                    {s.last_worker_id
                      ? <a style={{ color: "var(--text)" }} onClick={() => navigate("/workers")}>{s.last_worker_id}</a>
                      : <span className="muted">—</span>}
                  </dd>
                </dl>
              </div>
              <Btn
                size="sm"
                kind="ghost"
                icon="external"
                onClick={() => window.open("/v1/sessions/" + encodeURIComponent(s.id), "_blank", "noopener,noreferrer")}
              >View JSON</Btn>
            </div>
          </div>

          {/* Initial instructions */}
          {s.initial_instructions && (
            <div className="panel">
              <div className="panel-h">
                <span>Initial instructions</span>
                <div className="right">
                  <span className="muted text-sm">{s.initial_instructions.length} chars</span>
                </div>
              </div>
              <div className="panel-body" style={{ padding: 0 }}>
                <div className="code-block" style={{ border: "none", borderRadius: 0, background: "transparent" }}>
                  {s.initial_instructions}
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
                {s.status === "running" && (
                  <span className="text-sm mono" style={{ color: "var(--blue)" }}>● live</span>
                )}
              </div>
            </div>
            {turnsOpen && (
              <div className="panel-body">
                {turns.length === 0 ? (
                  <div className="muted text-sm" style={{ textAlign: "center", padding: 20 }}>
                    {s.status === "created"
                      ? "No turns yet — session is awaiting worker claim."
                      : "No turns reported on this session's row."}
                  </div>
                ) : (
                  <div className="turn-list">
                    {turns.map((t, i) => <TurnRow key={i} turn={t} index={i} />)}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Last error */}
          {s.last_error && (
            <div className="panel" style={{ borderColor: "oklch(0.7 0.2 25 / 0.4)" }}>
              <div className="panel-h" onClick={() => setErrorOpen(!errorOpen)} style={{ cursor: "pointer", background: "var(--red-dim)" }}>
                <Icon name={errorOpen ? "chevron-down" : "chevron-right"} size={12} style={{ color: "var(--red)" }} />
                <Icon name="x-circle" size={13} style={{ color: "var(--red)" }} />
                <span style={{ color: "var(--red)" }}>Last error</span>
                {s.last_error.type && <span className="mono sub">· {s.last_error.type}</span>}
                <div className="right">
                  {s.last_error.extensions?.request_id && (
                    <Btn
                      size="sm"
                      kind="ghost"
                      icon="copy"
                      onClick={(e) => { e.stopPropagation(); navigator.clipboard && navigator.clipboard.writeText(s.last_error.extensions.request_id); }}
                    >Copy request-id</Btn>
                  )}
                </div>
              </div>
              {errorOpen && (
                <div className="panel-body">
                  {s.last_error.title && <div style={{ fontWeight: 600, marginBottom: 4 }}>{s.last_error.title}</div>}
                  {s.last_error.detail && <div className="muted text-sm mb-3">{s.last_error.detail}</div>}
                  <div
                    className="code-block"
                    dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(s.last_error, null, 2)) }}
                  />
                </div>
              )}
            </div>
          )}

          {/* Metadata */}
          {s.metadata && Object.keys(s.metadata).length > 0 && (
            <div className="panel">
              <div className="panel-h" onClick={() => setMetaOpen(!metaOpen)} style={{ cursor: "pointer" }}>
                <Icon name={metaOpen ? "chevron-down" : "chevron-right"} size={12} className="muted" />
                <span>Metadata</span>
                <span className="sub">· {Object.keys(s.metadata).length} key{Object.keys(s.metadata).length === 1 ? "" : "s"}</span>
              </div>
              {metaOpen && (
                <div className="panel-body">
                  <dl className="kv" style={{ gridTemplateColumns: "180px 1fr" }}>
                    {Object.entries(s.metadata).map(([k, v]) => (
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
                disabled={s.status !== "running" || pauseMut.loading}
                icon="pause"
                onClick={onPause}
                title={s.status !== "running" ? "Enabled only when status = running" : ""}
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
                      <div className="muted text-sm">queued {relativeTime(_ageSec(q.at))}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* References */}
          <div className="panel">
            <div className="panel-h">
              <Icon name="fork" size={13} />
              <span>References</span>
            </div>
            <div className="panel-body" style={{ padding: "4px 14px" }}>
              {!isGraph && s.binding?.agent_id && (
                <div className="ref-row">
                  <Icon name="agent" size={13} className="ico" />
                  <span className="label">Agent</span>
                  <span className="val"><a onClick={() => navigate("/agents/" + s.binding.agent_id)}>{s.binding.agent_id}</a></span>
                </div>
              )}
              {isGraph && s.binding?.graph_id && (
                <div className="ref-row">
                  <Icon name="graph" size={13} className="ico" />
                  <span className="label">Graph</span>
                  <span className="val"><a onClick={() => navigate("/graphs/" + s.binding.graph_id)}>{s.binding.graph_id}</a></span>
                  <span className="pill pill-failed"><span className="dot"></span>executor missing</span>
                </div>
              )}
              <div className="ref-row">
                <Icon name="box" size={13} className="ico" />
                <span className="label">Workspace</span>
                <span className="val"><a onClick={() => navigate("/workspaces/" + wid)}>{wid}</a></span>
              </div>
              {s.last_worker_id && (
                <div className="ref-row">
                  <Icon name="worker" size={13} className="ico" />
                  <span className="label">Worker</span>
                  <span className="val"><a onClick={() => navigate("/workers")}>{s.last_worker_id}</a></span>
                </div>
              )}
            </div>
          </div>

          {/* Stale-cache notice — visible always per design §3.7 */}
          <div className="banner banner-info" style={{ background: "var(--bg-1)", color: "var(--text-3)", borderColor: "var(--border)" }}>
            <Icon name="info" size={14} className="ico" style={{ color: "var(--blue)" }} />
            <div style={{ flex: 1 }}>
              <div className="title" style={{ color: "var(--text)" }}>Reads are authoritative</div>
              <div className="detail" style={{ color: "var(--text-3)" }}>
                This view reads from <span className="mono" style={{ color: "var(--text)" }}>/v1/sessions/{`{id}`}</span>. The nested
                workspace path is known to drift after signals (T0399 / T0555 / T0611).
              </div>
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
              <Btn kind="danger" icon="stop" onClick={onCancel}>Cancel session</Btn>
            </>
          }
        >
          Sending a cancel signal to <strong className="mono" style={{ fontFamily: "inherit" }}>{s.id}</strong>.
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

function SessionDetailHeader({ sid, session, navigate }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="crumb">
          <a onClick={() => navigate("/sessions")}>Sessions</a>
          <span className="sep">/</span>
          <span className="mono" style={{ color: "var(--text)" }}>{sid}</span>
        </div>
        <h1 className="page-title mono">{sid}</h1>
        {session && (
          <div className="page-sub tabular">
            <StatusPill status={session.status} />
            <span style={{ marginLeft: 8 }} className="mono muted">{session.binding?.kind === "graph" ? "graph " : "agent "}{session.binding?.agent_id || session.binding?.graph_id || ""}</span>
          </div>
        )}
      </div>
      <div className="page-actions">
        <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/sessions")}>Back to list</Btn>
      </div>
    </div>
  );
}

function TurnRow({ turn, index }) {
  const [open, setOpen] = React.useState(turn.status === "running" || turn.status === "failed");
  const toolCalls = Array.isArray(turn.tool_calls) ? turn.tool_calls : [];
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
                {turn.tokens_in ?? 0} in · {turn.tokens_out ?? 0} out tokens · {toolCalls.length} tool call{toolCalls.length === 1 ? "" : "s"}
              </div>
            )}
            {toolCalls.map((tc, i) => (
              <div key={i} className="tool-call">
                <span className="name">{tc.name}</span>
                <span className="arrow">→</span>
                <span className="muted">{typeof tc.args === "object" ? JSON.stringify(tc.args) : (tc.args || "")}</span>
                {tc.ok !== false ? <span className="ok">✓ {tc.duration_ms ?? "?"}ms</span> : <span className="fail">✕ {tc.error || "failed"}</span>}
              </div>
            ))}
            {turn.output && <div className="code-block" style={{ marginTop: 6 }}>{turn.output}</div>}
          </>
        )}
      </div>
    </div>
  );
}

function _toastErr(pushToast, title) {
  return (err) => pushToast({
    kind: "error",
    title,
    detail: err.detail || err.title || err.message,
    requestId: err.requestId,
  });
}

window.SessionDetail = SessionDetail;
