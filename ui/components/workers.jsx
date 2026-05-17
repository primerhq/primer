/* global React, Icon, Btn, Modal, relativeTime */

const { apiFetch, useResource, useMutation, useRouter, useToast } = window.matrixApi;

// WorkersPage uses a dedicated cacheKey "workers:list" at 2000ms so its
// polling cadence doesn't fight the sidebar's 5000ms `sidebar:workers`
// subscription. The two are functionally the same query — extra
// bandwidth is bounded (one workers row over the wire).
//
// Per-row in_flight comes from /v1/sessions?worker_id={id}&status=running
// (the filter added in Backend Additions Task 2). Batched on each
// workers-list settle via a single useEffect.

function WorkersPage() {
  const { navigate } = useRouter();
  const { push: pushToast } = useToast();
  const workers = useResource(
    "workers:list",
    (signal) => apiFetch("GET", "/workers", null, { signal }),
    { pollMs: 2000 }
  );
  const health = useResource(
    "topbar:health",
    (signal) => apiFetch("GET", "/health", null, { signal }),
    { pollMs: 2000 }
  );

  // Per-worker derived state: in_flight + heartbeat-age tick.
  // Heartbeat age is a derived clock so it counts up even when no poll
  // has settled; we tick once per second to keep it fresh.
  const [, tick] = React.useState(0);
  React.useEffect(() => {
    const id = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // In-flight per worker: one fetch per worker on every workers-list
  // settle. Map keyed by worker id.
  const [perWorkerInFlight, setPerWorkerInFlight] = React.useState({});
  React.useEffect(() => {
    const items = workers.data?.items;
    if (!items || items.length === 0) return undefined;
    const ctrl = new AbortController();
    Promise.all(
      items.map((w) =>
        apiFetch(
          "GET",
          `/sessions?worker_id=${encodeURIComponent(w.id)}&status=running&limit=1`,
          null,
          { signal: ctrl.signal }
        )
          .then((r) => [w.id, r.total ?? 0])
          .catch(() => [w.id, null])
      )
    ).then((entries) => {
      setPerWorkerInFlight(Object.fromEntries(entries));
    });
    return () => ctrl.abort();
  }, [workers.data]);

  // Drain mutation. Invalidate both the page's cache and the sidebar /
  // topbar shared caches so the bell warning, sidebar count, and
  // topbar pill all reflect the new state immediately.
  const drainMut = useMutation(
    (id) => apiFetch("POST", `/workers/${encodeURIComponent(id)}/drain`),
    {
      invalidates: ["workers:list", "sidebar:workers", "topbar:health"],
      onSuccess: () => { /* toast triggered from confirm handler */ },
      onError: (err) => pushToast({
        kind: "error",
        title: "Drain failed",
        detail: err.detail || err.title || err.message,
        requestId: err.requestId,
      }),
    }
  );

  const [drainTarget, setDrainTarget] = React.useState(null);
  const [statusFilter, setStatusFilter] = React.useState("all");
  const [textFilter, setTextFilter] = React.useState("");

  const items = workers.data?.items ?? [];
  const filtered = items.filter((w) => {
    if (statusFilter !== "all" && w.status !== statusFilter) return false;
    if (textFilter) {
      const q = textFilter.toLowerCase();
      if (!w.id.toLowerCase().includes(q) && !(w.host || "").toLowerCase().includes(q)) return false;
    }
    return true;
  });

  const totalsFromHealth = health.data?.worker_pool || {};
  const totalInFlight = totalsFromHealth.in_flight ?? 0;
  const totalCapacity = totalsFromHealth.capacity ?? 0;
  const activeCount = items.filter((w) => w.status === "active").length;
  const drainingCount = items.filter((w) => w.status === "draining").length;
  const schedulerAlive = health.data?.scheduler?.alive === true;

  const confirmDrain = async () => {
    const target = drainTarget;
    setDrainTarget(null);
    try {
      await drainMut.mutate(target.id);
      pushToast({
        kind: "warning",
        title: `Draining ${target.id}`,
        detail: "In-flight sessions on this worker will finish before drain completes. New sessions won't be claimed.",
      });
    } catch (_e) {
      // onError above already pushed an error toast.
    }
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <WorkersHeader
        workersCount={items.length}
        inFlight={totalInFlight}
        capacity={totalCapacity}
        onRefresh={workers.refetch}
      />

      {/* Summary strip */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        <SummaryStat label="Total" value={items.length} sub="registered workers" />
        <SummaryStat
          label="Active"
          value={activeCount}
          sub={`${drainingCount} draining`}
          accent={activeCount === 0 ? "red" : "green"}
        />
        <SummaryStat
          label="In flight"
          value={totalCapacity > 0 ? `${totalInFlight} / ${totalCapacity}` : "—"}
          sub="claim utilization"
          accent={totalCapacity > 0 && totalInFlight / totalCapacity > 0.8 ? "amber" : "green"}
        />
        <SummaryStat
          label="Scheduler"
          value={schedulerAlive ? "alive" : (health.data ? "down" : "—")}
          sub={health.error ? "/v1/health error" : `last poll ${health.loading ? "in flight" : "just now"}`}
          accent={schedulerAlive ? "green" : "red"}
        />
      </div>

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter workers…"
            value={textFilter}
            onChange={(e) => setTextFilter(e.target.value)}
          />
        </div>
        <div className="sep-v" />
        <div className="chip-group">
          {["all", "active", "draining", "dead"].map((k) => (
            <span
              key={k}
              className={`chip ${statusFilter === k ? "active" : ""}`}
              onClick={() => setStatusFilter(k)}
              style={{ cursor: "pointer" }}
            >{k}</span>
          ))}
        </div>
        <span className="muted text-sm tabular" style={{ marginLeft: "auto" }}>
          <span className="mono" style={{ color: schedulerAlive ? "var(--green)" : "var(--red)" }}>● live</span>
          {" "}· /v1/workers every 2s
        </span>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 180 }}>ID</th>
              <th>Host / PID</th>
              <th>Status</th>
              <th style={{ width: 180 }}>Capacity</th>
              <th style={{ width: 130 }}>Last heartbeat</th>
              <th>Started</th>
              <th style={{ width: 80, textAlign: "right" }}></th>
            </tr>
          </thead>
          <tbody>
            {workers.loading && items.length === 0 ? (
              <tr><td colSpan={7} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading workers…</td></tr>
            ) : workers.error && items.length === 0 ? (
              <tr><td colSpan={7} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{workers.error.title || workers.error.message}</span>
                {" · "}<a onClick={workers.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={7} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                {items.length === 0
                  ? "No workers registered."
                  : `No workers match the current filter${textFilter ? ` "${textFilter}"` : ""}.`}
              </td></tr>
            ) : filtered.map((w) => (
              <WorkerRow
                key={w.id}
                w={w}
                inFlight={perWorkerInFlight[w.id]}
                onDrain={() => setDrainTarget(w)}
              />
            ))}
          </tbody>
        </table>
        <div className="tbl-foot">
          <span className="tabular">{filtered.length}{filtered.length !== items.length ? ` of ${items.length}` : ""} workers · polling every 2s</span>
        </div>
      </div>

      {drainTarget && (
        <Modal
          title={`Drain ${drainTarget.id}?`}
          danger
          onClose={() => setDrainTarget(null)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setDrainTarget(null)}>Cancel</Btn>
              <Btn kind="danger" icon="alert" onClick={confirmDrain}>Drain worker</Btn>
            </>
          }
        >
          Sending a drain signal to <strong className="mono" style={{ fontFamily: "inherit" }}>{drainTarget.id}</strong>.
          <ul>
            {(() => {
              const inf = perWorkerInFlight[drainTarget.id];
              const text = inf == null ? "Any in-flight sessions" : `${inf} in-flight session${inf === 1 ? "" : "s"}`;
              return <li><strong>{text}</strong> on this worker will finish before drain completes.</li>;
            })()}
            <li>The scheduler will stop assigning new sessions to this worker.</li>
            <li>The worker process exits cleanly once all turns finish.</li>
            <li>This action is idempotent — calling on an already-draining worker is a no-op.</li>
          </ul>
        </Modal>
      )}
    </div>
  );
}

function WorkersHeader({ workersCount, inFlight, capacity, onRefresh }) {
  const { navigate } = useRouter();
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <a onClick={() => navigate("/")}>Operations</a>
          <span className="sep">/</span>
          <span style={{ color: "var(--text)" }}>Workers</span>
        </div>
        <h1 className="page-title">Workers</h1>
        <div className="page-sub tabular">
          {workersCount} worker{workersCount === 1 ? "" : "s"}
          {capacity > 0 && (
            <>
              {" · "}<span className="mono" style={{ color: "var(--blue)" }}>{inFlight}</span>/{capacity} in flight
            </>
          )}
          <span className="mono" style={{ marginLeft: 4, color: "var(--text-3)" }}>· autorefresh every 2s</span>
        </div>
      </div>
      <div className="page-actions">
        <Btn icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>
      </div>
    </div>
  );
}

function WorkerRow({ w, inFlight, onDrain }) {
  // Heartbeat: app spec field is `last_heartbeat` (ISO string), NOT
  // `last_heartbeat_at` — pinned by T0080/T0307. Age in seconds from now.
  const lastHbMs = w.last_heartbeat ? new Date(w.last_heartbeat).getTime() : null;
  const ageSec = lastHbMs ? (Date.now() - lastHbMs) / 1000 : null;
  const heartbeatBad = ageSec != null && ageSec > 30;
  const ageStr = ageSec == null ? "—" : ageSec >= 60 ? `${Math.round(ageSec)}s ago` : `${ageSec.toFixed(1)}s ago`;

  return (
    <tr>
      <td className="mono" style={{ fontWeight: 500 }}>{w.id}</td>
      <td>
        <div className="mono">{w.host || "—"}</div>
        <div className="mono muted text-sm">pid {w.pid ?? "—"}</div>
      </td>
      <td>
        {w.status === "active" && <span className="pill pill-ended"><span className="dot"></span>active</span>}
        {w.status === "draining" && <span className="pill pill-paused"><span className="dot"></span>draining</span>}
        {w.status === "dead" && <span className="pill pill-failed"><span className="dot"></span>dead</span>}
        {!["active", "draining", "dead"].includes(w.status) && <span className="pill"><span className="dot"></span>{w.status}</span>}
      </td>
      <td>
        <CapacityBar inFlight={inFlight ?? 0} capacity={w.capacity || 0} />
        <div className="mono muted text-sm" style={{ marginTop: 2 }}>
          {inFlight ?? "—"} / {w.capacity ?? "—"}
        </div>
      </td>
      <td className={heartbeatBad ? "mono" : "mono muted"} style={{ color: heartbeatBad ? "var(--red)" : undefined }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{
            width: 6, height: 6, borderRadius: "50%",
            background: heartbeatBad ? "var(--red)" : (ageSec == null ? "var(--text-3)" : "var(--green)"),
            animation: ageSec == null ? "none" : "pulse 1.6s ease-in-out infinite",
          }}></span>
          {ageStr}
        </span>
      </td>
      <td className="mono muted">{w.started_at ? relativeTime((Date.now() - new Date(w.started_at).getTime()) / 1000) : "—"}</td>
      <td style={{ textAlign: "right", paddingRight: 12 }}>
        <Btn size="sm" kind="ghost" icon="alert" disabled={w.status !== "active"} onClick={onDrain}>Drain</Btn>
      </td>
    </tr>
  );
}

function CapacityBar({ inFlight, capacity }) {
  if (capacity <= 0) return <div className="muted text-sm">—</div>;
  const segments = Array.from({ length: Math.max(1, capacity) });
  return (
    <div style={{ display: "flex", gap: 2 }}>
      {segments.map((_, i) => {
        const filled = i < inFlight;
        return (
          <div
            key={i}
            style={{
              flex: 1,
              height: 8,
              borderRadius: 2,
              background: filled ? (inFlight / capacity > 0.8 ? "var(--amber)" : "var(--accent)") : "var(--bg-2)",
              border: "1px solid " + (filled ? "transparent" : "var(--border)"),
            }}
          />
        );
      })}
    </div>
  );
}

function SummaryStat({ label, value, sub, accent }) {
  const color = accent === "red" ? "var(--red)" : accent === "amber" ? "var(--amber)" : accent === "green" ? "var(--green)" : undefined;
  return (
    <div className="panel">
      <div className="panel-body" style={{ padding: "12px 14px" }}>
        <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>{label}</div>
        <div className="mono tabular" style={{ fontSize: 22, fontWeight: 600, marginTop: 4, letterSpacing: "-0.02em", color }}>{value}</div>
        <div className="muted text-sm" style={{ marginTop: 2 }}>{sub}</div>
      </div>
    </div>
  );
}

window.WorkersPage = WorkersPage;
