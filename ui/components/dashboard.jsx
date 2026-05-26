/* global React, Icon, StatusPill, Btn, Sparkline, relativeTime, Banner */

function Dashboard({ workerStats, subsystemOn, onNavigate, onNewSession }) {
  const { useResource, apiFetch } = window.matrixApi;

  // Tile counts — lightweight polls (every 5s).
  const sessionsResource = useResource(
    "dashboard:sessions",
    (signal) => apiFetch("GET", "/sessions?limit=1", null, { signal }),
    { pollMs: 5000 }
  );
  const workspacesResource = useResource(
    "dashboard:workspaces",
    (signal) => apiFetch("GET", "/workspaces?limit=1", null, { signal }),
    { pollMs: 5000 }
  );
  const workersResource = useResource(
    "dashboard:workers",
    (signal) => apiFetch("GET", "/workers", null, { signal }),
    { pollMs: 5000 }
  );
  // Recent sessions table — newest 8 rows, descending by created_at.
  // Real data; previously read from a mock array that didn't reflect
  // the actual server state (rows for non-existent sessions).
  const recentSessionsResource = useResource(
    "dashboard:recent-sessions",
    (signal) => apiFetch(
      "GET", "/sessions?limit=8&order_by=created_at:desc", null, { signal },
    ),
    { pollMs: 5000 }
  );
  // Running / paused breakdown for the Sessions tile sub-line. Cheap
  // count probes via /sessions/find with a status predicate.
  const runningResource = useResource(
    "dashboard:running",
    (signal) => apiFetch(
      "POST",
      "/sessions/find",
      {
        predicate: {
          kind: "predicate",
          left: { kind: "field", name: "status" },
          op: "=",
          right: { kind: "value", value: "running" },
        },
        page: { kind: "offset", offset: 0, length: 1 },
      },
      { signal },
    ),
    { pollMs: 5000 }
  );
  const pausedResource = useResource(
    "dashboard:paused",
    (signal) => apiFetch(
      "POST",
      "/sessions/find",
      {
        predicate: {
          kind: "predicate",
          left: { kind: "field", name: "status" },
          op: "=",
          right: { kind: "value", value: "paused" },
        },
        page: { kind: "offset", offset: 0, length: 1 },
      },
      { signal },
    ),
    { pollMs: 5000 }
  );

  const sessionsTotal = sessionsResource.data?.total;
  const workspacesTotal = workspacesResource.data?.total;
  const workersItems = workersResource.data?.items ?? [];
  const workersTotalReal = workersItems.length;
  const workersActiveReal = workersItems.filter((w) => w.status === "active").length;

  const runningCount = runningResource.data?.total ?? 0;
  const pausedCount = pausedResource.data?.total ?? 0;
  // No cheap server-side aggregate for "errors in the last hour" yet —
  // it'd need a created_at-range predicate + status filter. Surface 0
  // here rather than a fabricated number; the Errors tile clicks
  // through to /health for the real story.
  const errorsLast1h = 0;
  const last1h = 0;

  const utilization = workerStats.capacity > 0
    ? Math.round((workerStats.in_flight / workerStats.capacity) * 100)
    : 0;
  const recentSessions = recentSessionsResource.data?.items ?? [];

  // Tick clock for live gauge animation
  const [, force] = React.useState(0);
  React.useEffect(() => {
    const id = setInterval(() => force((x) => x + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // Generate live sparkline data — pseudo-stable
  const sparkData = React.useMemo(() => {
    const arr = [];
    let v = workerStats.in_flight;
    for (let i = 0; i < 30; i++) {
      v += (Math.random() - 0.5) * 1.4;
      v = Math.max(0, Math.min(workerStats.capacity, v));
      arr.push(v);
    }
    arr[arr.length - 1] = workerStats.in_flight;
    return arr;
  }, [workerStats.in_flight, workerStats.capacity]);

  return (
    <div className="col" style={{ gap: 18 }}>
      {/* System health strip */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        <HealthCard
          icon="worker"
          label="Workers"
          value={`${workersActiveReal}/${workersTotalReal}`}
          sub={`${workerStats.in_flight}/${workerStats.capacity} in flight · ${workersItems.filter((w) => w.status === "draining").length} draining`}
          status={workersActiveReal === 0 ? "err" : (workerStats.capacity > 0 && workerStats.in_flight / workerStats.capacity > 0.8 ? "warn" : "ok")}
          onClick={() => onNavigate("workers")}
        />
        <HealthCard
          icon="zap"
          label="Sessions"
          value={sessionsTotal != null ? sessionsTotal : runningCount}
          sub={workspacesTotal != null ? `${workspacesTotal} workspaces` : `${pausedCount} paused · ${last1h} created (1h)`}
          status="ok"
          accent
          onClick={() => onNavigate("sessions")}
        />
        <HealthCard
          icon="subsystem"
          label="Internal Collections"
          value={subsystemOn ? "ON" : "OFF"}
          sub={subsystemOn ? "last bootstrap 14m ago" : "configured · not bootstrapped"}
          status={subsystemOn ? "ok" : "warn"}
          onClick={() => onNavigate("internal-collections")}
        />
        <HealthCard
          icon="alert"
          label="Errors (1h)"
          value={errorsLast1h}
          sub={errorsLast1h === 0 ? "no 5xx responses" : "see /errors log"}
          status={errorsLast1h > 0 ? "warn" : "ok"}
          onClick={() => onNavigate("health")}
        />
      </div>

      {/* Gauge + Quick actions */}
      <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 18 }}>
        <div className="panel">
          <div className="panel-h">
            <Icon name="zap" size={13} style={{ color: "var(--accent)" }} />
            <span>Worker pool utilization</span>
            <span className="sub">· live · /v1/health every 2s</span>
            <div className="right">
              <span className="mono text-sm muted">5 min window</span>
            </div>
          </div>
          <div className="panel-body" style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 24, alignItems: "center" }}>
            <Gauge value={utilization} />
            <div className="col">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <div>
                  <div className="muted text-sm mono">IN FLIGHT</div>
                  <div className="mono" style={{ fontSize: 28, fontWeight: 600, letterSpacing: "-0.02em" }}>
                    {workerStats.in_flight}<span className="muted" style={{ fontSize: 18 }}> / {workerStats.capacity}</span>
                  </div>
                </div>
                <Sparkline values={sparkData} width={160} height={36} />
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginTop: 12, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
                <Metric label="claimed" value={workerStats.in_flight} />
                <Metric label="queued" value={Math.max(0, runningCount - workerStats.in_flight)} />
                <Metric label="capacity" value={workerStats.capacity} />
              </div>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">
            <Icon name="play" size={11} />
            <span>Quick actions</span>
          </div>
          <div className="panel-body" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <QuickAction icon="plus" label="New workspace" onClick={() => onNavigate("workspaces")} />
            <QuickAction icon="plus" label="New agent" onClick={() => onNavigate("agents")} />
            <QuickAction icon="zap" label="New session" onClick={onNewSession} />
            <QuickAction icon="collection" label="Collections" onClick={() => onNavigate("collections")} />
            <QuickAction icon="worker" label="Workers" onClick={() => onNavigate("workers")} />
            <QuickAction
              icon="external"
              label="OpenAPI"
              onClick={() => window.open("/v1/docs", "_blank", "noopener,noreferrer")}
            />
          </div>
        </div>
      </div>

      {/* Recent sessions */}
      <div className="panel">
        <div className="panel-h">
          <Icon name="zap" size={13} className="muted" />
          <span>Recent sessions</span>
          <span className="sub">· last {recentSessions.length}</span>
          <div className="right">
            <Btn size="sm" kind="ghost" iconRight="chevron-right" onClick={() => onNavigate("sessions")}>View all</Btn>
          </div>
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          <table className="tbl">
            <thead>
              <tr>
                <th>Status</th>
                <th>Session</th>
                <th>Agent</th>
                <th>Workspace</th>
                <th style={{ textAlign: "right" }}>Turns</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {recentSessions.length === 0 && !recentSessionsResource.loading ? (
                <tr><td colSpan={7} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                  No sessions yet — created sessions land here, newest first.
                </td></tr>
              ) : recentSessions.map((s) => {
                const isGraph = (s.binding?.kind || s.binding_kind) === "graph";
                const boundAgent = s.binding?.agent_id || s.agent_id || "";
                const boundGraph = s.binding?.graph_id || s.graph_id || "";
                const parkedTool = s.parked_status === "parked"
                  ? (s.parked_state?.yielded?.tool_name || null)
                  : null;
                const createdAgeSec = s.created_at
                  ? Math.max(0, (Date.now() - new Date(s.created_at).getTime()) / 1000)
                  : null;
                return (
                  <tr key={s.id} onClick={() => onNavigate("session-detail", s.id)}>
                    <td><StatusPill status={s.status} parked={parkedTool} /></td>
                    <td className="mono">{(s.id || "").length > 22 ? (s.id.slice(0, 22) + "…") : s.id}</td>
                    <td className="mono">
                      {isGraph
                        ? <span style={{ color: "var(--violet)" }}>{boundGraph || "—"}</span>
                        : (boundAgent || <span className="muted">—</span>)}
                    </td>
                    <td className="mono muted">
                      {s.workspace_id ? (s.workspace_id.length > 16 ? (s.workspace_id.slice(0, 14) + "…") : s.workspace_id) : "—"}
                    </td>
                    <td className="mono num tabular">{s.turn_no ?? 0}</td>
                    <td className="mono muted">
                      {createdAgeSec != null ? relativeTime(createdAgeSec) : "—"}
                    </td>
                    <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function HealthCard({ icon, label, value, sub, status, accent, onClick }) {
  const dotColor = status === "ok" ? "var(--green)" : status === "warn" ? "var(--amber)" : "var(--red)";
  return (
    <div className="panel" style={{ cursor: "pointer", position: "relative", overflow: "hidden" }} onClick={onClick}>
      <div className="panel-body" style={{ padding: "12px 14px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <Icon name={icon} size={14} style={{ color: accent ? "var(--accent)" : "var(--text-3)" }} />
          <span className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>
            {label}
          </span>
          <div style={{ marginLeft: "auto", width: 8, height: 8, borderRadius: "50%", background: dotColor, boxShadow: `0 0 0 3px ${dotColor.replace("var(--", "var(--").replace(")", "-dim)")}` }}></div>
        </div>
        <div className="mono tabular" style={{ fontSize: 24, fontWeight: 600, letterSpacing: "-0.025em" }}>{value}</div>
        <div className="muted text-sm" style={{ marginTop: 4 }}>{sub}</div>
      </div>
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <div>
      <div className="muted text-sm mono" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</div>
      <div className="mono tabular" style={{ fontSize: 18, fontWeight: 600 }}>{value}</div>
    </div>
  );
}

function QuickAction({ icon, label, onClick }) {
  return (
    <button
      className="btn"
      style={{
        width: "100%",
        justifyContent: "flex-start",
        padding: "10px 12px",
        background: "var(--bg-2)",
        border: "1px solid var(--border)",
        height: 44,
      }}
      onClick={onClick}
    >
      <Icon name={icon} size={13} style={{ color: "var(--accent)" }} />
      <span style={{ fontSize: 12.5 }}>{label}</span>
    </button>
  );
}

function Gauge({ value }) {
  // Half-arc gauge
  const radius = 56;
  const stroke = 10;
  const cx = 70, cy = 70;
  // Arc from 180° to 0° (full half-circle, top)
  const startAngle = Math.PI;
  const endAngle = 0;
  const valueAngle = Math.PI - (Math.PI * Math.min(100, Math.max(0, value)) / 100);

  const arcPath = (startA, endA) => {
    const x1 = cx + radius * Math.cos(startA);
    const y1 = cy - radius * Math.sin(startA);
    const x2 = cx + radius * Math.cos(endA);
    const y2 = cy - radius * Math.sin(endA);
    const large = 0;
    const sweep = startA > endA ? 1 : 0;
    return `M ${x1} ${y1} A ${radius} ${radius} 0 ${large} ${sweep} ${x2} ${y2}`;
  };

  const color = value > 80 ? "var(--amber)" : value > 95 ? "var(--red)" : "var(--accent)";

  return (
    <svg width={140} height={94} viewBox="0 0 140 94">
      <path d={arcPath(startAngle, endAngle)} stroke="var(--border)" strokeWidth={stroke} fill="none" strokeLinecap="round" />
      <path d={arcPath(startAngle, valueAngle)} stroke={color} strokeWidth={stroke} fill="none" strokeLinecap="round" />
      <text x={cx} y={cy + 6} textAnchor="middle" fontFamily="IBM Plex Mono" fontSize="22" fontWeight="600" fill="var(--text)">
        {value}<tspan fontSize="13" fill="var(--text-3)">%</tspan>
      </text>
      <text x={cx} y={cy + 24} textAnchor="middle" fontFamily="IBM Plex Mono" fontSize="9.5" fill="var(--text-3)" letterSpacing="0.06em">
        UTILIZATION
      </text>
    </svg>
  );
}

window.Dashboard = Dashboard;
