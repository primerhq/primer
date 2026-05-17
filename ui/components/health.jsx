/* global React, Icon, Btn */

const { apiFetch, useResource, useRouter } = window.matrixApi;

// HealthPage owns its own page header (breadcrumb + title + sub) per
// Foundation Task 10 Step 1's final-sub-bullet refactor strategy.
// Reads from `useResource("topbar:health")` so the topbar pill, the
// dashboard utilization gauge, and this page all share one /v1/health
// poll cycle.
//
// Sparkline buffer is an in-memory ring keyed to component mount:
// navigating away resets it, per parent UI spec §5.11.
function HealthPage() {
  const { navigate } = useRouter();
  const health = useResource(
    "topbar:health",
    (signal) => apiFetch("GET", "/health", null, { signal }),
    { pollMs: 5000 }
  );

  // Ring buffer of in_flight samples; 60 = ~5 min at the 5s cadence.
  const buffer = React.useRef([]);
  const [bufLen, setBufLen] = React.useState(0);
  React.useEffect(() => {
    const inFlight = health.data?.worker_pool?.in_flight;
    if (inFlight == null) return;
    buffer.current.push(inFlight);
    if (buffer.current.length > 60) buffer.current.shift();
    setBufLen(buffer.current.length);
  }, [health.data]);

  const wp = health.data?.worker_pool || {};
  const sched = health.data?.scheduler || {};
  const status = health.data?.status;

  const ok =
    status === "ok" &&
    sched.alive === true &&
    wp.capacity != null;
  const heroTitle = ok
    ? "All systems operational"
    : status == null
      ? "Reading /v1/health…"
      : !sched.alive
        ? "Scheduler not alive"
        : wp.capacity == null
          ? "Worker pool not attached"
          : "Degraded";

  return (
    <div className="col" style={{ gap: 18 }}>
      <HealthHeader onRefresh={health.refetch} />

      {/* Status hero */}
      <div className="panel" style={{
        background: ok ? "linear-gradient(90deg, var(--green-dim) 0%, var(--bg-1) 60%)" : "linear-gradient(90deg, var(--red-dim) 0%, var(--bg-1) 60%)",
        borderColor: ok ? "oklch(0.75 0.15 145 / 0.3)" : "oklch(0.7 0.2 25 / 0.3)",
      }}>
        <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 16, padding: "18px 22px" }}>
          <div style={{
            width: 56, height: 56, borderRadius: 12,
            background: ok ? "var(--green)" : "var(--red)",
            display: "grid", placeItems: "center",
            boxShadow: `0 0 0 4px ${ok ? "var(--green-dim)" : "var(--red-dim)"}`,
          }}>
            <Icon name={ok ? "check" : "x"} size={28} style={{ color: "var(--accent-fg)" }} />
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 600, letterSpacing: "-0.01em" }}>
              {heroTitle}
            </div>
            <div className="muted text-sm" style={{ marginTop: 2 }}>
              <span className="mono">GET /v1/health</span>
              {health.data && <> · <span className="mono" style={{ color: "var(--text)" }}>200 OK</span></>}
              {health.error && <> · <span className="mono" style={{ color: "var(--red)" }}>error: {health.error.title || health.error.message}</span></>}
              {health.data?.version && <> · v<span className="mono">{health.data.version}</span></>}
            </div>
          </div>
          <Btn icon="refresh" kind="ghost" onClick={health.refetch}>Refresh now</Btn>
        </div>
      </div>

      {/* in_flight sparkline */}
      <div className="panel">
        <div className="panel-h">
          <Icon name="zap" size={13} style={{ color: "var(--accent)" }} />
          <span>in_flight</span>
          <span className="sub">· last 5 min · 5s tick · client-side only</span>
          <div className="right">
            <span className="mono tabular" style={{ fontSize: 20, fontWeight: 600 }}>{wp.in_flight ?? "—"}</span>
            <span className="muted mono"> / {wp.capacity ?? "—"}</span>
          </div>
        </div>
        <div className="panel-body" style={{ padding: "16px 14px" }}>
          {bufLen === 0 ? (
            <div className="muted text-sm" style={{ padding: "24px 0", textAlign: "center" }}>
              Collecting samples…
            </div>
          ) : (
            <BigSpark values={buffer.current} capacity={wp.capacity ?? 1} />
          )}
        </div>
      </div>

      {/* Two-column metric tables driven by whatever the backend exposes */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
        <MetricsPanel
          title="Scheduler"
          icon="settings"
          prominent={[
            { k: "alive", v: String(sched.alive ?? "—"), emphasis: sched.alive ? "green" : "red" },
          ]}
          metrics={sched.metrics || {}}
        />
        <MetricsPanel
          title="Worker pool"
          icon="worker"
          prominent={[
            {
              k: "in_flight",
              v: wp.in_flight ?? "—",
              emphasis: wp.capacity > 0 && wp.in_flight >= wp.capacity * 0.8 ? "amber" : null,
            },
            { k: "capacity", v: wp.capacity ?? "—" },
            {
              k: "utilization",
              v: wp.capacity > 0 ? Math.round((wp.in_flight / wp.capacity) * 100) + "%" : "—",
            },
          ]}
          metrics={wp.metrics || {}}
        />
      </div>

      {/* Footer hint about ephemerality */}
      <div className="muted text-sm" style={{ textAlign: "center", padding: "4px 0" }}>
        Sparkline buffer is in-memory; resets on navigation.
      </div>
    </div>
  );
}

function HealthHeader({ onRefresh }) {
  const { navigate } = useRouter();
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <a onClick={() => navigate("/")}>Operations</a>
          <span className="sep">/</span>
          <span style={{ color: "var(--text)" }}>Health</span>
        </div>
        <h1 className="page-title">Health</h1>
        <div className="page-sub">
          Live <span className="mono">/v1/health</span> · poll every 5s · client-side history
        </div>
      </div>
      <div className="page-actions">
        <Btn icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>
      </div>
    </div>
  );
}

function MetricsPanel({ title, icon, prominent, metrics }) {
  // Render prominent rows first (alive, in_flight, capacity, utilization)
  // then iterate every backend-supplied metric key in sorted order.
  const dynamicRows = Object.entries(metrics)
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([k, v]) => ({ k, v: typeof v === "number" ? v : String(v) }));
  const allRows = [...prominent, ...dynamicRows];
  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name={icon} size={13} className="muted" />
        <span>{title}</span>
        <span className="sub">· {dynamicRows.length} metric{dynamicRows.length === 1 ? "" : "s"}</span>
      </div>
      <div className="panel-body" style={{ padding: 0 }}>
        <table className="tbl" style={{ borderCollapse: "collapse" }}>
          <tbody>
            {allRows.length === 0 ? (
              <tr>
                <td colSpan={2} className="muted text-sm" style={{ padding: "16px 14px", textAlign: "center" }}>
                  No metrics reported.
                </td>
              </tr>
            ) : allRows.map((r) => {
              const color = r.emphasis === "green" ? "var(--green)" : r.emphasis === "amber" ? "var(--amber)" : r.emphasis === "red" ? "var(--red)" : undefined;
              return (
                <tr key={r.k} style={{ cursor: "default" }}>
                  <td className="mono muted" style={{ width: "60%" }}>{r.k}</td>
                  <td className="mono num tabular" style={{ color, fontWeight: r.emphasis ? 600 : 400 }}>
                    {r.v}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BigSpark({ values, capacity }) {
  const w = 100;
  const h = 30;
  const max = Math.max(...values, capacity);
  const min = 0;
  const range = max - min || 1;
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const pts = values.map((v, i) => [i * step, h - 2 - ((v - min) / range) * (h - 4)]);
  const path = pts.map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(" ");
  const area = `${path} L${w},${h} L0,${h} Z`;
  const capY = h - 2 - ((capacity - min) / range) * (h - 4);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: "100%", height: 140, display: "block" }}>
      {[0.25, 0.5, 0.75].map((f, i) => (
        <line key={i} x1="0" x2={w} y1={h - 2 - f * (h - 4)} y2={h - 2 - f * (h - 4)} stroke="var(--border)" strokeWidth="0.3" strokeDasharray="0.6 0.6" />
      ))}
      <line x1="0" x2={w} y1={capY} y2={capY} stroke="var(--amber)" strokeWidth="0.4" strokeDasharray="0.8 0.8" />
      <path d={area} fill="var(--accent-dim)" stroke="none" vectorEffect="non-scaling-stroke" />
      <path d={path} stroke="var(--accent)" strokeWidth="1.2" fill="none" vectorEffect="non-scaling-stroke" />
      {pts.length > 0 && (
        <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r="1.4" fill="var(--accent)" />
      )}
    </svg>
  );
}

window.HealthPage = HealthPage;
