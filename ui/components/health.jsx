/* global React, Icon, Btn, Sparkline */

function HealthPage({ sessions }) {
  const { useResource, useViewport, apiFetch } = window.primerApi;
  const { isMobile } = useViewport();
  const health = useResource(
    "health:root",
    (signal) => apiFetch("GET", "/health", null, { signal }),
    { pollMs: 5000 }
  );

  const data = health.data || {};
  const wp = data.worker_pool || {};
  const sched = data.scheduler || {};
  const status = data.status;
  const inFlight = typeof wp.in_flight === "number" ? wp.in_flight : 0;
  const capacity = typeof wp.capacity === "number" && wp.capacity > 0 ? wp.capacity : 0;

  // Client-side history for in_flight — seeded zero, appended on each poll.
  const [history, setHistory] = React.useState(() => Array.from({ length: 60 }, () => 0));
  React.useEffect(() => {
    if (health.data == null) return;
    setHistory((h) => [...h.slice(-59), inFlight]);
  }, [health.data, inFlight]);

  const ok = status === "ok" && sched.alive === true;
  const schedMetrics = sched.metrics || {};
  const poolMetricsRaw = wp.metrics || {};

  // Build display rows defensively — show "—" for metrics not present in
  // the snapshot (depends on scheduler/worker pool implementation).
  const fmt = (v) => (v == null || v === "" ? "—" : v);
  const schedulerMetrics = [
    { k: "alive", v: sched.alive === true ? "true" : "false", emphasis: sched.alive ? "green" : "red" },
    { k: "claims_total", v: fmt(schedMetrics["primer_scheduler_claims_total"]) },
    { k: "claim_latency_p50_ms", v: fmt(schedMetrics["primer_scheduler_claim_latency_p50_ms"]) },
    { k: "claim_latency_p99_ms", v: fmt(schedMetrics["primer_scheduler_claim_latency_p99_ms"]) },
    { k: "missed_heartbeats_total", v: fmt(schedMetrics["primer_scheduler_missed_heartbeats_total"]) },
    { k: "scheduler_loops_total", v: fmt(schedMetrics["primer_scheduler_loops_total"]) },
  ];

  const poolMetrics = [
    { k: "in_flight", v: inFlight, emphasis: capacity > 0 && inFlight / capacity > 0.8 ? "amber" : null },
    { k: "capacity_total", v: capacity || "—" },
    { k: "sessions_completed_total", v: fmt(poolMetricsRaw["primer_worker_sessions_completed_total"]) },
    { k: "sessions_failed_total", v: fmt(poolMetricsRaw["primer_worker_sessions_failed_total"]) },
    { k: "turns_executed_total", v: fmt(poolMetricsRaw["primer_worker_turns_executed_total"]) },
    { k: "turn_duration_p50_s", v: fmt(poolMetricsRaw["primer_worker_turn_duration_p50_s"]) },
    { k: "turn_duration_p99_s", v: fmt(poolMetricsRaw["primer_worker_turn_duration_p99_s"]) },
  ];

  return (
    <div className="col" style={{ gap: 18 }}>
      {/* Top status */}
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
              {ok ? "All systems operational" : (health.data ? "Degraded" : (health.error ? "Health probe failed" : "Reading /v1/health…"))}
            </div>
            <div className="muted text-sm" style={{ marginTop: 2 }}>
              <span className="mono">GET /v1/health</span>
              {health.data && <> · <span className="mono" style={{ color: "var(--text)" }}>200 OK</span></>}
              {health.error && <> · <span className="mono" style={{ color: "var(--red)" }}>error: {health.error.title || health.error.message}</span></>}
              {data.version && <> · v<span className="mono">{data.version}</span></>}
            </div>
          </div>
          <Btn icon="refresh" kind="ghost" onClick={health.refetch}>Refresh now</Btn>
        </div>
      </div>

      {/* Live in-flight chart */}
      <div className="panel">
        <div className="panel-h">
          <Icon name="zap" size={13} style={{ color: "var(--accent)" }} />
          <span>in_flight</span>
          <span className="sub">· last 5 min · 5s tick · client-side only</span>
          <div className="right">
            <span className="mono tabular" style={{ fontSize: 20, fontWeight: 600 }}>{inFlight}</span>
            <span className="muted mono">/ {capacity || "—"}</span>
          </div>
        </div>
        <div className="panel-body" style={{ padding: "16px 14px" }}>
          <BigSpark values={history} capacity={capacity || 1} />
        </div>
      </div>

      {/* Two-column metrics */}
      <div className={`metric-grid ${isMobile ? "metric-grid-mobile" : ""}`} style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
        <MetricsPanel title="Scheduler" icon="settings" rows={schedulerMetrics} />
        <MetricsPanel title="Worker pool" icon="worker" rows={poolMetrics} />
      </div>

      {health.error && (
        <div className="panel" style={{ borderColor: "var(--red)" }}>
          <div className="panel-body" style={{ padding: "12px 14px" }}>
            <div className="mono" style={{ color: "var(--red)" }}>
              {health.error.title || health.error.message || "Health probe failed"}
            </div>
            {health.error.detail && (
              <div className="muted text-sm" style={{ marginTop: 4 }}>{health.error.detail}</div>
            )}
            {health.error.requestId && (
              <div className="muted text-sm mono" style={{ marginTop: 4 }}>
                request-id <span style={{ color: "var(--text)" }}>{health.error.requestId}</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function MetricsPanel({ title, icon, rows }) {
  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name={icon} size={13} className="muted" />
        <span>{title}</span>
      </div>
      <div className="panel-body" style={{ padding: 0 }}>
        <table className="tbl" style={{ borderCollapse: "collapse" }}>
          <tbody>
            {rows.map((r) => {
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
  const step = w / (values.length - 1);
  const pts = values.map((v, i) => [i * step, h - 2 - ((v - min) / range) * (h - 4)]);
  const path = pts.map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(" ");
  const area = `${path} L${w},${h} L0,${h} Z`;
  const capY = h - 2 - ((capacity - min) / range) * (h - 4);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: "100%", height: 140, display: "block" }}>
      {/* Y-axis grid */}
      {[0.25, 0.5, 0.75].map((f, i) => (
        <line key={i} x1="0" x2={w} y1={h - 2 - f * (h - 4)} y2={h - 2 - f * (h - 4)} stroke="var(--border)" strokeWidth="0.3" strokeDasharray="0.6 0.6" />
      ))}
      {/* Capacity line */}
      <line x1="0" x2={w} y1={capY} y2={capY} stroke="var(--amber)" strokeWidth="0.4" strokeDasharray="0.8 0.8" />
      <path d={area} fill="var(--accent-dim)" stroke="none" vectorEffect="non-scaling-stroke" />
      <path d={path} stroke="var(--accent)" strokeWidth="1.2" fill="none" vectorEffect="non-scaling-stroke" />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r="1.4" fill="var(--accent)" />
    </svg>
  );
}

window.HealthPage = HealthPage;
