/* global React, Icon, StatusPill, Btn, Sparkline, relativeTime */

const { apiFetch, useResource, useRouter } = window.matrixApi;

// Same 404-suppression shape as chrome.jsx (the IC config endpoint
// returns 404 when the subsystem is OFF — that's the documented
// signal, not an error).
async function _fetchIcConfig(signal) {
  try {
    return await apiFetch("GET", "/internal_collections/config", null, { signal });
  } catch (err) {
    if (err && err.status === 404) return null;
    throw err;
  }
}

function Dashboard({ onNewSession }) {
  const { navigate } = useRouter();

  // Shared caches with topbar/sidebar — same /v1/health, /v1/workers,
  // /v1/internal_collections/config polls; no duplicate traffic.
  const health = useResource(
    "topbar:health",
    (s) => apiFetch("GET", "/health", null, { signal: s }),
    { pollMs: 2000 }
  );
  const workers = useResource(
    "sidebar:workers",
    (s) => apiFetch("GET", "/workers", null, { signal: s }),
    { pollMs: 5000 }
  );
  const ic = useResource("sidebar:ic-config", _fetchIcConfig, { pollMs: 30000 });

  // Recent sessions for the bottom table. The default order from the
  // list endpoint is created_at desc per the existing tests.
  const recent = useResource(
    "dashboard:recent-sessions",
    (s) => apiFetch("GET", "/sessions?limit=8", null, { signal: s }),
    { pollMs: 5000 }
  );

  // Worker pool sparkline — in-memory ring buffer, last 5 min at 2s = 150.
  const sparkBuffer = React.useRef([]);
  const [, sparkTick] = React.useState(0);
  React.useEffect(() => {
    const inf = health.data?.worker_pool?.in_flight;
    if (inf == null) return;
    sparkBuffer.current.push(inf);
    if (sparkBuffer.current.length > 150) sparkBuffer.current.shift();
    sparkTick((x) => x + 1);
  }, [health.data]);

  const wp = health.data?.worker_pool || {};
  const sched = health.data?.scheduler || {};
  const items = workers.data?.items ?? [];
  const activeWorkers = items.filter((w) => w.status === "active").length;
  const drainingWorkers = items.filter((w) => w.status === "draining").length;
  const totalWorkers = items.length;
  const inFlight = wp.in_flight ?? 0;
  const capacity = wp.capacity ?? 0;
  const utilization = capacity > 0 ? Math.round((inFlight / capacity) * 100) : 0;

  const sessionsTotal = recent.data?.total ?? null;
  const sessionRows = recent.data?.items ?? [];
  const runningCount = sessionRows.filter((s) => s.status === "running").length;
  const pausedCount = sessionRows.filter((s) => s.status === "paused").length;

  const subsystemOn = ic.data != null;

  return (
    <div className="col" style={{ gap: 18 }}>
      <DashboardHeader onNewSession={onNewSession} />

      {/* Health strip */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        <HealthCard
          icon="worker"
          label="Workers"
          value={totalWorkers === 0 ? "—" : `${activeWorkers}/${totalWorkers}`}
          sub={
            capacity > 0
              ? `${inFlight}/${capacity} in flight${drainingWorkers ? ` · ${drainingWorkers} draining` : ""}`
              : "no worker pool attached"
          }
          status={
            !sched.alive || capacity === 0 || activeWorkers === 0 ? "err"
            : capacity > 0 && inFlight >= capacity * 0.8 ? "warn"
            : "ok"
          }
          onClick={() => navigate("/workers")}
        />
        <HealthCard
          icon="zap"
          label="Sessions"
          value={sessionsTotal == null ? "—" : sessionsTotal}
          sub={`${runningCount} running · ${pausedCount} paused (top ${sessionRows.length})`}
          status="ok"
          accent
          onClick={() => navigate("/sessions")}
        />
        <HealthCard
          icon="subsystem"
          label="Internal Collections"
          value={subsystemOn ? "ON" : "OFF"}
          sub={subsystemOn ? "configured" : "not configured"}
          status={subsystemOn ? "ok" : "warn"}
          onClick={() => navigate("/subsystems/internal-collections")}
        />
        <HealthCard
          icon="alert"
          label="Errors (1h)"
          value="—"
          sub="endpoint not implemented (planned)"
          status="ok"
          onClick={() => navigate("/health")}
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
                    {inFlight}
                    <span className="muted" style={{ fontSize: 18 }}> / {capacity || "—"}</span>
                  </div>
                </div>
                {sparkBuffer.current.length > 0 ? (
                  <Sparkline values={sparkBuffer.current} width={160} height={36} />
                ) : (
                  <span className="muted text-sm">collecting…</span>
                )}
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginTop: 12, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
                <Metric label="in flight" value={inFlight} />
                <Metric label="capacity" value={capacity || "—"} />
                <Metric label="active workers" value={activeWorkers} />
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
            <QuickAction icon="plus" label="New workspace" onClick={() => navigate("/workspaces", { create: "1" })} />
            <QuickAction icon="plus" label="New agent" onClick={() => navigate("/agents", { create: "1" })} />
            <QuickAction icon="zap" label="New session" onClick={onNewSession} />
            <QuickAction icon="search" label="Search bench" onClick={() => navigate("/knowledge/search")} />
            <QuickAction icon="external" label="OpenAPI" onClick={() => window.open("/v1/openapi.json", "_blank", "noopener,noreferrer")} />
            <QuickAction icon="heart" label="Health" onClick={() => navigate("/health")} />
          </div>
        </div>
      </div>

      {/* Recent sessions */}
      <div className="panel">
        <div className="panel-h">
          <Icon name="zap" size={13} className="muted" />
          <span>Recent sessions</span>
          <span className="sub">· last {sessionRows.length}{sessionsTotal != null && sessionRows.length < sessionsTotal ? ` of ${sessionsTotal}` : ""}</span>
          <div className="right">
            <Btn size="sm" kind="ghost" iconRight="chevron-right" onClick={() => navigate("/sessions")}>View all</Btn>
          </div>
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {recent.loading && sessionRows.length === 0 ? (
            <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
          ) : recent.error && sessionRows.length === 0 ? (
            <div style={{ padding: 20, textAlign: "center" }}>
              <span style={{ color: "var(--red)" }}>{recent.error.title || recent.error.message}</span>
              {" · "}<a onClick={recent.refetch} style={{ cursor: "pointer" }}>Retry</a>
            </div>
          ) : sessionRows.length === 0 ? (
            <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
              No sessions yet · <a onClick={onNewSession} style={{ cursor: "pointer", color: "var(--accent)" }}>+ New session</a>
            </div>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Session</th>
                  <th>Agent / Graph</th>
                  <th>Workspace</th>
                  <th>Created</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {sessionRows.map((s) => (
                  <tr key={s.id} onClick={() => navigate("/sessions/" + s.id)} style={{ cursor: "pointer" }}>
                    <td><StatusPill status={s.status} /></td>
                    <td className="mono">{s.id.slice(0, 24)}{s.id.length > 24 && <span className="muted">…</span>}</td>
                    <td className="mono">
                      {s.binding?.kind === "graph"
                        ? <span style={{ color: "var(--violet)" }}>{s.binding.graph_id}</span>
                        : s.binding?.agent_id || "—"}
                    </td>
                    <td className="mono muted">{(s.workspace_id || "").slice(0, 18)}{s.workspace_id && s.workspace_id.length > 18 && "…"}</td>
                    <td className="mono muted">{s.created_at ? relativeTime((Date.now() - new Date(s.created_at).getTime()) / 1000) : "—"}</td>
                    <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

function DashboardHeader({ onNewSession }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span style={{ color: "var(--text)" }}>Dashboard</span>
        </div>
        <h1 className="page-title">Dashboard</h1>
        <div className="page-sub">Operator overview · live <span className="mono">/v1/health</span> + <span className="mono">/v1/sessions</span></div>
      </div>
      <div className="page-actions">
        <Btn icon="external" kind="ghost" onClick={() => window.open("/v1/openapi.json", "_blank", "noopener,noreferrer")}>View OpenAPI</Btn>
        <Btn icon="plus" kind="primary" onClick={onNewSession}>New session</Btn>
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
  const radius = 56;
  const stroke = 10;
  const cx = 70, cy = 70;
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
