/* global React, Icon, StatusPill, Btn, Modal, Banner, relativeTime, fmtDate */

function SessionDetail({ session, onBack, pushToast, onPatchSession }) {
  const [steer, setSteer] = React.useState("");
  const [showCancel, setShowCancel] = React.useState(false);
  const [queuedInstructions, setQueuedInstructions] = React.useState([]);
  const [turnsOpen, setTurnsOpen] = React.useState(true);
  const [errorOpen, setErrorOpen] = React.useState(true);
  const [metaOpen, setMetaOpen] = React.useState(false);

  if (!session) return null;
  const isTerminal = ["ended", "failed", "cancelled"].includes(session.status);
  const isGraph = session.binding_kind === "graph";
  const isParked = session.parked_status === "parked";
  const parkedToolName = isParked ? session.parked_state.yielded.tool_name : null;

  // Generate fake turns from session
  const turns = generateTurns(session);

  const handlePause = () => {
    onPatchSession(session.id, { status: "paused" });
    pushToast({ kind: "success", title: "Session paused", detail: "The worker will halt after the current turn completes." });
  };
  const handleResume = () => {
    if (session.status === "running") {
      pushToast({ kind: "info", title: "Already running", detail: "Resume is idempotent — returned 200 no-op." });
      return;
    }
    onPatchSession(session.id, { status: "running" });
    pushToast({ kind: "success", title: "Session resumed", detail: "Scheduler will hand it to the next available worker." });
  };
  const handleCancel = () => {
    setShowCancel(false);
    onPatchSession(session.id, { status: "cancelled" });
    pushToast({ kind: "warning", title: "Cancel signal sent", detail: "May take up to ~30 s if the worker is mid-turn." });
  };
  const handleCancelYield = () => {
    if (!isParked) return;
    onPatchSession(session.id, { parked_status: null, parked_state: null });
    pushToast({ kind: "warning", title: "Yield cancelled", detail: `POST /v1/sessions/${session.id}/yields/${session.parked_state.tool_call_id}/cancel · reason=operator-skipped` });
  };
  const handleAskUserRespond = (payload) => {
    onPatchSession(session.id, { parked_status: null, parked_state: null });
    pushToast({ kind: "success", title: "Response sent", detail: `Session resumed with ${typeof payload === "string" ? `"${payload.slice(0, 32)}${payload.length > 32 ? "…" : ""}"` : "structured payload"}.` });
  };
  const handleSteer = () => {
    if (!steer.trim()) return;
    setQueuedInstructions((q) => [...q, { text: steer, at: new Date() }]);
    pushToast({ kind: "success", title: "Steer queued", detail: "The instruction was appended to the session's pending queue." });
    setSteer("");
  };

  return (
    <div className="col">
      {isGraph && (
        <Banner
          kind="warning"
          icon="alert"
          title="Graph executor is unimplemented"
          detail="This session is bound to a graph. The graph executor currently raises NotImplementedError, so the session ends with `failed` on the first turn. Pinned in app spec §12."
          actions={<Btn size="sm" kind="ghost">Open spec</Btn>}
        />
      )}

      {isParked && parkedToolName === "ask_user" && (
        <AskUserPanel session={session} onRespond={handleAskUserRespond} onSkip={handleCancelYield} pushToast={pushToast} />
      )}
      {/* Tool-approval pending — pick the first pending approval scoped to this session */}
      {(window.PENDING_APPROVALS || []).filter((a) => a.scope.kind === "session" && a.scope.id === session.id).slice(0, 1).map((a) => (
        <ApprovalBanner
          key={a.tool_call_id}
          approval={a}
          onApprove={() => pushToast({ kind: "success", title: "Approved", detail: `POST /v1/sessions/${session.id}/tool_approval/respond → 202` })}
          onReject={(reason) => pushToast({ kind: "warning", title: "Rejected", detail: `"${reason}"` })}
        />
      ))}
      {isParked && parkedToolName === "watch_files" && (
        <WatchFilesPanel session={session} onCancelYield={handleCancelYield} />
      )}
      {isParked && parkedToolName === "sleep" && (
        <SleepPanel session={session} onCancelYield={handleCancelYield} />
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
                  <StatusPill status={session.status} parked={parkedToolName} />
                  <span className="muted text-sm">
                    {isParked && (
                      <>parked on <span className="mono" style={{ color: "var(--amber)" }}>{parkedToolName}</span> · {relativeTime((Date.now() - session.parked_state.parked_at.getTime()) / 1000)}</>
                    )}
                    {!isParked && session.status === "running" && `turn ${session.turn_count} · started ${relativeTime((Date.now() - session.started_at.getTime()) / 1000)}`}
                    {!isParked && session.status === "paused" && `paused at turn ${session.turn_count}`}
                    {!isParked && session.status === "created" && "awaiting worker claim"}
                    {!isParked && session.status === "ended" && `completed ${session.turn_count} turns`}
                    {!isParked && session.status === "failed" && "failed during execution"}
                    {!isParked && session.status === "cancelled" && "cancelled by operator"}
                  </span>
                </div>
                <dl className="kv">
                  <dt>bound</dt>
                  <dd>
                    {isGraph ? (
                      <>graph · <a style={{ color: "var(--violet)" }}>{session.graph_id}</a></>
                    ) : (
                      <>agent · <a style={{ color: "var(--accent)" }}>{session.agent_id}</a></>
                    )}
                  </dd>
                  <dt>workspace</dt>
                  <dd><a style={{ color: "var(--text)", cursor: "pointer" }}>{session.workspace_id}</a></dd>
                  <dt>created</dt>
                  <dd>{fmtDate(session.created_at)} <span className="muted">· {relativeTime((Date.now() - session.created_at.getTime()) / 1000)}</span></dd>
                  {session.started_at && (
                    <>
                      <dt>started</dt>
                      <dd>{fmtDate(session.started_at)}</dd>
                    </>
                  )}
                  {session.last_turn_at && (
                    <>
                      <dt>last turn</dt>
                      <dd>
                        {fmtDate(session.last_turn_at)}{" "}
                        <span className="muted">· {relativeTime((Date.now() - session.last_turn_at.getTime()) / 1000)}</span>
                      </dd>
                    </>
                  )}
                  <dt>attempt</dt>
                  <dd>{session.attempt}</dd>
                  <dt>worker</dt>
                  <dd>{session.worker_id ? <a style={{ color: "var(--text)" }}>{session.worker_id}</a> : <span className="muted">—</span>}</dd>
                </dl>
              </div>
              <Btn size="sm" kind="ghost" icon="external">View JSON</Btn>
            </div>
          </div>

          {/* Initial instructions */}
          <div className="panel">
            <div className="panel-h">
              <span>Initial instructions</span>
              <div className="right">
                <span className="muted text-sm">{session.instructions.length} chars</span>
              </div>
            </div>
            <div className="panel-body" style={{ padding: 0 }}>
              <div className="code-block" style={{ border: "none", borderRadius: 0, background: "transparent" }}>
                {session.instructions}
              </div>
            </div>
          </div>

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
                    No turns yet — session is awaiting worker claim.
                  </div>
                ) : (
                  <div className="turn-list">
                    {turns.map((t, i) => (
                      <Turn key={i} turn={t} index={i} />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Last error */}
          {session.error && (
            <div className="panel" style={{ borderColor: "oklch(0.7 0.2 25 / 0.4)" }}>
              <div className="panel-h" onClick={() => setErrorOpen(!errorOpen)} style={{ cursor: "pointer", background: "var(--red-dim)" }}>
                <Icon name={errorOpen ? "chevron-down" : "chevron-right"} size={12} style={{ color: "var(--red)" }} />
                <Icon name="x-circle" size={13} style={{ color: "var(--red)" }} />
                <span style={{ color: "var(--red)" }}>Last error</span>
                <span className="mono sub">· {session.error.type}</span>
                <div className="right">
                  <Btn size="sm" kind="ghost" icon="copy">Copy request-id</Btn>
                </div>
              </div>
              {errorOpen && (
                <div className="panel-body">
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>{session.error.title}</div>
                  <div className="muted text-sm mb-3">{session.error.detail}</div>
                  <div className="code-block">
                    <button className="copy"><Icon name="copy" size={10} /> copy</button>
                    <span className="com">{"// RFC 7807 envelope"}</span>{"\n"}
                    {"{"}{"\n"}
                    {"  "}<span className="key">"type"</span>{": "}<span className="str">"{session.error.type}"</span>,{"\n"}
                    {"  "}<span className="key">"title"</span>{": "}<span className="str">"{session.error.title}"</span>,{"\n"}
                    {"  "}<span className="key">"status"</span>{": "}<span className="num">{session.error.status}</span>,{"\n"}
                    {"  "}<span className="key">"detail"</span>{": "}<span className="str">"{session.error.detail}"</span>,{"\n"}
                    {"  "}<span className="key">"instance"</span>{": "}<span className="str">"{session.error.instance}"</span>,{"\n"}
                    {"  "}<span className="key">"extensions"</span>{": "}{JSON.stringify(session.error.extensions, null, 2).split("\n").map((l, i) => i === 0 ? l : "  " + l).join("\n")}{"\n"}
                    {"}"}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Metadata */}
          <div className="panel">
            <div className="panel-h" onClick={() => setMetaOpen(!metaOpen)} style={{ cursor: "pointer" }}>
              <Icon name={metaOpen ? "chevron-down" : "chevron-right"} size={12} className="muted" />
              <span>Metadata</span>
              <span className="sub">· 4 keys</span>
              <div className="right">
                <Btn size="sm" kind="ghost" icon="plus">Add key</Btn>
              </div>
            </div>
            {metaOpen && (
              <div className="panel-body">
                <dl className="kv" style={{ gridTemplateColumns: "180px 1fr" }}>
                  <dt>source</dt><dd>"webhook"</dd>
                  <dt>customer_id</dt><dd>"cust_8d2a"</dd>
                  <dt>priority</dt><dd className="num">3</dd>
                  <dt>tags</dt><dd>["billing","refund"]</dd>
                </dl>
              </div>
            )}
          </div>
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
                disabled={session.status !== "running" || isParked}
                icon="pause"
                onClick={handlePause}
                title={session.status !== "running" ? "Enabled only when status = running" : isParked ? "Cancel the yield first" : ""}
              >
                Pause
              </Btn>
              <Btn icon="play" onClick={handleResume} title="Idempotent — returns 200 no-op if already running" disabled={isParked}>
                Resume
              </Btn>
              {isParked && (
                <Btn
                  kind="ghost"
                  icon="x"
                  onClick={handleCancelYield}
                  title={`Cancel the ${parkedToolName} yield and let the session continue`}
                  style={{ color: "var(--amber)" }}
                >
                  Cancel yield
                </Btn>
              )}
              <Btn
                kind="danger"
                disabled={isTerminal}
                icon="stop"
                onClick={() => setShowCancel(true)}
              >
                End session
              </Btn>
              <div style={{ borderTop: "1px solid var(--border)", margin: "4px -14px 0" }} />

              <div className="field-label mt-2" style={{ marginBottom: 4 }}>
                Steer instruction
                <span className="hint">does not gate on status</span>
              </div>
              <textarea
                className="textarea mono"
                placeholder='Drop a hint or new directive for the next turn…'
                value={steer}
                onChange={(e) => setSteer(e.target.value)}
                rows={3}
                style={{ fontSize: 12 }}
              />
              <Btn kind="primary" icon="send" onClick={handleSteer} disabled={!steer.trim()}>
                Queue steer
              </Btn>

              {queuedInstructions.length > 0 && (
                <div style={{ marginTop: 8 }}>
                  <div className="field-label" style={{ marginBottom: 4 }}>Queued ({queuedInstructions.length})</div>
                  {queuedInstructions.map((q, i) => (
                    <div key={i} className="tool-call" style={{ flexDirection: "column", alignItems: "flex-start" }}>
                      <div style={{ color: "var(--text)", fontFamily: "inherit" }}>{q.text}</div>
                      <div className="muted text-sm">queued {relativeTime((Date.now() - q.at.getTime()) / 1000)}</div>
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
              {!isGraph ? (
                <>
                  <div className="ref-row">
                    <Icon name="agent" size={13} className="ico" />
                    <span className="label">Agent</span>
                    <span className="val"><a>{session.agent_id}</a></span>
                    <span className="pill pill-ended"><span className="dot"></span>ok</span>
                  </div>
                  <div className="ref-row">
                    <Icon name="llm" size={13} className="ico" />
                    <span className="label">LLM</span>
                    <span className="val">openai-1 <span className="muted">· gpt-4o</span></span>
                    <span className="pill pill-ended"><span className="dot"></span>ok</span>
                  </div>
                  <div className="ref-row">
                    <Icon name="tools" size={13} className="ico" />
                    <span className="label">Toolset</span>
                    <span className="val">_workspaces <span className="muted">· 8 tools</span></span>
                    <span className="pill pill-ended"><span className="dot"></span>ok</span>
                  </div>
                  <div className="ref-row">
                    <Icon name="tools" size={13} className="ico" />
                    <span className="label">Toolset</span>
                    <span className="val">stripe-mcp <span className="muted">· 14 tools</span></span>
                    <span className="pill pill-ended"><span className="dot"></span>ok</span>
                  </div>
                </>
              ) : (
                <div className="ref-row">
                  <Icon name="graph" size={13} className="ico" />
                  <span className="label">Graph</span>
                  <span className="val"><a>{session.graph_id}</a></span>
                  <span className="pill pill-failed"><span className="dot"></span>executor missing</span>
                </div>
              )}
              <div className="ref-row">
                <Icon name="box" size={13} className="ico" />
                <span className="label">Workspace</span>
                <span className="val"><a>{session.workspace_id.slice(0, 16)}…</a></span>
                <span className="pill pill-ended"><span className="dot"></span>active</span>
              </div>
              {session.worker_id && (
                <div className="ref-row">
                  <Icon name="worker" size={13} className="ico" />
                  <span className="label">Worker</span>
                  <span className="val"><a>{session.worker_id}</a></span>
                  <span className="pill pill-claimed"><span className="dot"></span>claimed</span>
                </div>
              )}
            </div>
          </div>

          {/* Stale-cache notice */}
          <div className="banner banner-info" style={{ background: "var(--bg-1)", color: "var(--text-3)", borderColor: "var(--border)" }}>
            <Icon name="info" size={14} className="ico" style={{ color: "var(--blue)" }} />
            <div style={{ flex: 1 }}>
              <div className="title" style={{ color: "var(--text)" }}>Reads are authoritative</div>
              <div className="detail" style={{ color: "var(--text-3)" }}>
                This view reads from <span className="mono" style={{ color: "var(--text)" }}>/v1/sessions/{`{id}`}</span>. The nested
                workspace path can drift after signals (T0399 / T0555 / T0611).
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
              <Btn kind="danger" icon="stop" onClick={handleCancel}>Cancel session</Btn>
            </>
          }
        >
          Sending a cancel signal to <strong className="mono" style={{ fontFamily: "inherit" }}>{session.id}</strong>.
          <ul>
            <li>The worker will finish or abandon the current turn — this may take up to ~30 s.</li>
            <li>Any queued steer instructions will be discarded.</li>
            <li>The workspace and its <span className="mono" style={{ fontSize: 11 }}>.state</span> are not affected.</li>
          </ul>
        </Modal>
      )}
    </div>
  );
}

function Turn({ turn, index }) {
  const [open, setOpen] = React.useState(turn.status === "running" || turn.status === "failed");
  return (
    <div className={`turn ${turn.status}`}>
      <div className="turn-dot">{turn.status === "running" ? <Icon name="zap" size={11} /> : index + 1}</div>
      <div className="turn-body">
        <div className="turn-h" onClick={() => setOpen(!open)} style={{ cursor: "pointer" }}>
          <Icon name={open ? "chevron-down" : "chevron-right"} size={11} className="muted" />
          <span>Turn {index + 1}</span>
          <span className="time">· {turn.startedAt}</span>
          <span className="dur">· {turn.duration}</span>
          {turn.status === "running" && <span className="pill pill-running" style={{ marginLeft: 4 }}><span className="dot"></span>running</span>}
          {turn.status === "failed" && <span className="pill pill-failed" style={{ marginLeft: 4 }}><span className="dot"></span>failed</span>}
        </div>
        {open && (
          <>
            <div className="turn-meta">
              {turn.tokensIn} in · {turn.tokensOut} out tokens · {turn.toolCalls.length} tool call{turn.toolCalls.length === 1 ? "" : "s"}
            </div>
            {turn.toolCalls.map((tc, i) => (
              <div key={i} className="tool-call">
                <span className="name">{tc.name}</span>
                <span className="arrow">→</span>
                <span className="muted">{tc.args}</span>
                {tc.ok ? <span className="ok">✓ {tc.ms}ms</span> : <span className="fail">✕ {tc.error}</span>}
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

function generateTurns(session) {
  const n = session.turn_count;
  if (n === 0) return [];
  const turns = [];
  const samples = {
    "support-triage": [
      { tools: [["read_email", "msg_id=im_8d2a", true, 124], ["zendesk.search_tickets", "subject~='refund'", true, 412]], out: "Identified as a refund request under $50 — routing to billing queue." },
      { tools: [["zendesk.create_ticket", "queue=billing, priority=normal", true, 318]], out: "Created ticket #BT-4218 in billing queue." },
      { tools: [["zendesk.add_comment", "ticket=BT-4218", true, 187]], out: "Drafted response; awaiting approval before send." },
    ],
    "pr-reviewer": [
      { tools: [["github.get_pr", "number=4218", true, 220], ["fs.read_files", "diff", true, 92]], out: "Read 14 files (1,847 lines). Two areas to focus on." },
      { tools: [["github.add_review_comment", "file=db/0017_*.sql line=42", true, 156], ["github.add_review_comment", "file=workers/scheduler.py line=180", true, 142]], out: "Posted 2 inline comments. Migration looks safe; retry logic has a race on cancellation." },
    ],
    "stripe-refunds": [
      { tools: [["stripe.search_charges", "charge_id=ch_3OZ4mQ", true, 218]], out: "Found charge — verifying double-billing claim." },
    ],
    "code-explainer": [
      { tools: [["fs.read", "path=scheduler.py", true, 64]], out: "Read scheduler.py (412 lines)." },
      { tools: [["fs.read", "path=worker.py", true, 58], ["fs.read", "path=storage.py", true, 71]], out: "Read worker.py and storage.py." },
      { tools: [], out: "Explained the claim→run_turn→commit flow with diagrams." },
      { tools: [["fs.grep", "pattern='SELECT.*sessions'", true, 124]], out: "Found the 3 SQL queries that mutate session state." },
      { tools: [], out: "Walked through the row-level locking strategy in detail." },
    ],
    "doc-ingestion": [
      { tools: [["fs.ls", "path=docs/", true, 18]], out: "Discovered 142 markdown files." },
      { tools: [["fs.read", "batch=1/4", true, 480], ["search.ingest", "collection=docs chunks=380", true, 1840]], out: "Ingested batch 1/4." },
      { tools: [["fs.read", "batch=2/4", true, 510], ["search.ingest", "collection=docs chunks=410", true, 1920]], out: "Ingested batch 2/4." },
      { tools: [["fs.read", "batch=3/4", true, 490], ["search.ingest", "collection=docs chunks=395", true, 1810]], out: "Ingested batch 3/4." },
    ],
    "sql-helper": [
      { tools: [["db.introspect", "table=events", true, 88]], out: "", failed: true, errorText: "provider 502" },
    ],
    "release-notes": [
      { tools: [["git.log", "abc123..def456", true, 142]], out: "Found 28 commits in range." },
    ],
  };
  const samp = samples[session.agent_id] || samples["support-triage"];
  for (let i = 0; i < n; i++) {
    const s = samp[i % samp.length];
    const sec = 1000 * (n - i) * 60;
    const isLast = i === n - 1;
    const status = session.status === "running" && isLast ? "running" :
                   session.status === "failed" && isLast ? "failed" :
                   s.failed ? "failed" : "ok";
    turns.push({
      status,
      startedAt: fmtDate(new Date(Date.now() - sec)).slice(11),
      duration: status === "running" ? "—" : `${(Math.random() * 5 + 1).toFixed(1)}s`,
      tokensIn: Math.floor(Math.random() * 2000 + 500),
      tokensOut: Math.floor(Math.random() * 800 + 100),
      toolCalls: s.tools.map(([name, args, ok, ms]) => ({ name, args, ok, ms, error: ok ? null : "timeout" })),
      output: status === "running" ? "" : (s.failed ? null : s.out),
    });
  }
  return turns;
}

window.SessionDetail = SessionDetail;

// =================================================================
// Yielding-tools UI surfaces (spec A.1, A.3, A.4)
// =================================================================

function AskUserPanel({ session, onRespond, onSkip }) {
  const meta = session.parked_state.yielded.resume_metadata || {};
  const prompt = meta.prompt || "";
  const schema = meta.response_schema;
  const isLong = prompt.length > 80 || !!schema;

  const [text, setText] = React.useState("");
  const [json, setJson] = React.useState("{\n  \n}");
  const [err, setErr] = React.useState(null);

  const jsonParsed = React.useMemo(() => {
    if (!schema) return null;
    try { return { ok: true, value: JSON.parse(json) }; }
    catch (e) { return { ok: false, error: e.message }; }
  }, [json, schema]);

  const canSend = schema
    ? (jsonParsed && jsonParsed.ok && hasRequired(jsonParsed.value, schema))
    : text.trim().length > 0;

  const submit = () => {
    setErr(null);
    if (schema) {
      if (!jsonParsed.ok) { setErr(`Invalid JSON: ${jsonParsed.error}`); return; }
      const missing = (schema.required || []).filter((k) => !(k in jsonParsed.value));
      if (missing.length > 0) { setErr(`Missing required field${missing.length === 1 ? "" : "s"}: ${missing.join(", ")}`); return; }
      if (jsonParsed.value.queue === "invalid") {
        setErr(`queue: value is not a valid enumeration member; permitted: 'billing', 'technical', 'sales'`);
        return;
      }
      onRespond(jsonParsed.value);
    } else {
      onRespond(text);
    }
  };

  return (
    <div className="panel" style={{ borderColor: "var(--amber)", boxShadow: "0 0 0 3px var(--amber-dim)" }}>
      <div className="panel-h" style={{ background: "var(--amber-dim)" }}>
        <Icon name="warn-circle" size={14} style={{ color: "var(--amber)" }} />
        <span style={{ color: "var(--amber)" }}>Agent needs input</span>
        <span className="mono sub">· ask_user · {session.parked_state.tool_call_id}</span>
        <div className="right">
          <span className="muted text-sm tabular">polling <span className="mono">/ask_user/pending</span> · 2s</span>
        </div>
      </div>
      <div className="panel-body">
        <div className="field" style={{ marginBottom: 12 }}>
          <label className="field-label" style={{ color: "var(--text)" }}>
            Prompt {schema && <span className="hint">· schema-driven response</span>}
          </label>
          <div style={{ padding: "10px 12px", background: "var(--bg-2)", borderRadius: 6, border: "1px solid var(--border)", fontSize: 13, lineHeight: 1.5 }}>{prompt}</div>
        </div>

        {schema ? (
          <>
            <div className="field" style={{ marginBottom: 8 }}>
              <label className="field-label">response schema</label>
              <div className="code-block" style={{ maxHeight: 140, overflow: "auto" }}>
                {JSON.stringify(schema, null, 2)}
              </div>
            </div>
            <div className="field">
              <label className="field-label">response <span className="hint">json · validated client-side</span></label>
              <textarea
                className="textarea mono"
                value={json}
                onChange={(e) => setJson(e.target.value)}
                rows={6}
                style={{ fontSize: 12 }}
                placeholder={`{\n  "queue": "billing",\n  "priority": 3\n}`}
              />
              {jsonParsed && !jsonParsed.ok && json.trim().length > 2 && (
                <div className="field-help" style={{ color: "var(--red)" }}>
                  <Icon name="x-circle" size={11} style={{ verticalAlign: -1, marginRight: 3 }} />
                  {jsonParsed.error}
                </div>
              )}
            </div>
          </>
        ) : isLong ? (
          <div className="field">
            <label className="field-label">your response</label>
            <textarea
              className="textarea"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              placeholder="Type your reply…"
              autoFocus
            />
          </div>
        ) : (
          <div className="field">
            <label className="field-label">your response</label>
            <input
              className="input"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && canSend) submit(); }}
              placeholder="Type your reply…"
              autoFocus
              style={{ width: "100%" }}
            />
          </div>
        )}

        {err && (
          <div className="field-help" style={{ color: "var(--red)", marginTop: 0, marginBottom: 10 }}>
            <Icon name="x-circle" size={11} style={{ verticalAlign: -1, marginRight: 3 }} />
            {err}
          </div>
        )}

        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
          <Btn kind="primary" icon="send" disabled={!canSend} onClick={submit}>Send response</Btn>
          <Btn kind="ghost" icon="x" onClick={onSkip} title="POST /yields/{tool_call_id}/cancel">Skip</Btn>
          <div style={{ marginLeft: "auto" }} className="muted text-sm">
            POST <span className="mono">/v1/sessions/{session.id.slice(0, 16)}…/ask_user/respond</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function hasRequired(obj, schema) {
  if (!schema || !schema.required) return true;
  return schema.required.every((k) => k in obj);
}

function WatchFilesPanel({ session, onCancelYield }) {
  const meta = session.parked_state.yielded.resume_metadata || {};
  const paths = meta.paths || [];
  const win = meta.coalesce_window_ms;
  const parkedSec = (Date.now() - session.parked_state.parked_at.getTime()) / 1000;

  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name="search" size={13} style={{ color: "var(--amber)" }} />
        <span style={{ color: "var(--amber)" }}>Watching</span>
        <span className="mono sub">· watch_files · {session.parked_state.tool_call_id}</span>
        <div className="right">
          <Btn size="sm" kind="ghost" icon="x" onClick={onCancelYield}>Cancel yield</Btn>
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
          <span className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 10.5 }}>coalesce</span>
          <span className="mono">{win}ms</span>
          <span className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 10.5 }}>parked</span>
          <span className="mono">{relativeTime(parkedSec)}</span>
        </div>
      </div>
    </div>
  );
}

function SleepPanel({ session, onCancelYield }) {
  const meta = session.parked_state.yielded.resume_metadata || {};
  const duration = meta.duration_s || 0;
  const resumeAt = meta.resume_at ? new Date(meta.resume_at) : null;
  const elapsed = (Date.now() - session.parked_state.parked_at.getTime()) / 1000;
  const remaining = Math.max(0, duration - elapsed);
  const pct = Math.min(100, (elapsed / Math.max(1, duration)) * 100);
  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name="clock" size={13} style={{ color: "var(--amber)" }} />
        <span style={{ color: "var(--amber)" }}>Sleeping</span>
        <span className="mono sub">· sleep · {session.parked_state.tool_call_id}</span>
        <div className="right">
          <Btn size="sm" kind="ghost" icon="x" onClick={onCancelYield}>Cancel yield</Btn>
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

window.AskUserPanel = AskUserPanel;
window.WatchFilesPanel = WatchFilesPanel;
window.SleepPanel = SleepPanel;
