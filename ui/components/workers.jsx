/* global React, Icon, Btn, Modal, relativeTime, fmtDate */

function WorkersPage({ sessions, pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const [drainTarget, setDrainTarget] = React.useState(null);
  const [, tick] = React.useState(0);
  // Tick heartbeats live
  React.useEffect(() => {
    const id = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const list = useResource(
    "workers:list",
    (signal) => apiFetch("GET", "/workers", null, { signal }),
    { pollMs: 2000 }
  );
  const workers = (list.data?.items ?? []).map((w) => ({
    // Real WorkerInfo: {id, host, pid, capacity, started_at, last_heartbeat, status}
    // The Designer's row expects: in_flight, heartbeat (seconds-ago number).
    ...w,
    in_flight: typeof w.in_flight === "number" ? w.in_flight : 0,
    heartbeat: w.last_heartbeat
      ? Math.max(0, (Date.now() - new Date(w.last_heartbeat).getTime()) / 1000)
      : 0,
  }));

  // Capture the most-recent drain target so onSuccess / onError can
  // include the id in the toast (useMutation invokes hooks with `data`
  // only — no echo of the request body).
  const drainTargetRef = React.useRef(null);
  const drainMut = useMutation(
    (id) => {
      drainTargetRef.current = id;
      return apiFetch("POST", `/workers/${encodeURIComponent(id)}/drain`);
    },
    {
      invalidates: ["workers:list"],
      onSuccess: () =>
        pushToast({
          kind: "warning",
          title: `Draining ${drainTargetRef.current || ""}`.trim(),
          detail:
            "In-flight sessions on this worker will finish before drain completes. New sessions won't be claimed.",
        }),
      onError: (err) =>
        pushToast({
          kind: "error",
          title: err.title || "Drain failed",
          detail: err.detail,
          requestId: err.requestId,
        }),
    }
  );

  const totals = workers.reduce(
    (acc, w) => {
      acc.cap += w.capacity || 0;
      acc.flight += w.in_flight || 0;
      if (w.status === "active") acc.active += 1;
      if (w.status === "draining") acc.draining += 1;
      return acc;
    },
    { cap: 0, flight: 0, active: 0, draining: 0 }
  );

  const onDrain = (w) => setDrainTarget(w);
  const confirmDrain = () => {
    const w = drainTarget;
    setDrainTarget(null);
    if (w) drainMut.mutate(w.id);
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      {/* Summary strip */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        <SummaryStat label="Total" value={workers.length} sub="registered workers" />
        <SummaryStat label="Active" value={totals.active} sub={`${totals.draining} draining`} accent={totals.active === 0 ? "red" : "green"} />
        <SummaryStat label="In flight" value={`${totals.flight} / ${totals.cap}`} sub="claim utilization" accent={totals.cap > 0 && totals.flight / totals.cap > 0.8 ? "amber" : "green"} />
        <SummaryStat label="Scheduler" value="alive" sub="last claim 2s ago" accent="green" />
      </div>

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter workers…" />
        </div>
        <div className="sep-v" />
        <div className="chip-group">
          <span className="chip active">all</span>
          <span className="chip">active</span>
          <span className="chip">draining</span>
          <span className="chip">dead</span>
        </div>
        <span className="muted text-sm tabular" style={{ marginLeft: "auto" }}>
          <span className="mono" style={{ color: "var(--green)" }}>● live</span> · /v1/workers every 2s
        </span>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 110 }}>ID</th>
              <th>Host / PID</th>
              <th>Status</th>
              <th style={{ width: 180 }}>Capacity</th>
              <th style={{ width: 130 }}>Last heartbeat</th>
              <th>Started</th>
              <th style={{ width: 70, textAlign: "right" }}></th>
            </tr>
          </thead>
          <tbody>
            {workers.map((w) => (
              <WorkerRow
                key={w.id}
                w={w}
                sessions={sessions || []}
                onDrain={onDrain}
                draining={drainMut.loading}
              />
            ))}
          </tbody>
        </table>
        <div className="tbl-foot">
          <span className="tabular">{workers.length} workers · polling every 2s</span>
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
            <li><strong>{drainTarget.in_flight} in-flight session{drainTarget.in_flight === 1 ? "" : "s"}</strong> on this worker will finish before drain completes.</li>
            <li>The scheduler will stop assigning new sessions to this worker.</li>
            <li>The worker process exits cleanly once all turns finish.</li>
            <li>This action is idempotent — calling on an already-draining worker is a no-op.</li>
          </ul>
        </Modal>
      )}
    </div>
  );
}

function WorkerRow({ w, sessions, onDrain, draining }) {
  const onWorker = sessions.filter(
    (s) => s.worker_id === w.id && (s.status === "running" || s.status === "paused")
  );
  const heartbeatAge = w.heartbeat ?? 0;
  const heartbeatBad = heartbeatAge > 30;
  const capacity = w.capacity || 0;
  const inFlight = w.in_flight || 0;
  // Real WorkerInfo.started_at is an ISO string — relativeTime expects
  // seconds-ago. Compute the delta defensively.
  const startedAtSecondsAgo = w.started_at
    ? Math.max(0, (Date.now() - new Date(w.started_at).getTime()) / 1000)
    : null;

  return (
    <tr>
      <td className="mono" style={{ fontWeight: 500 }}>{w.id}</td>
      <td>
        <div className="mono">{w.host}</div>
        <div className="mono muted text-sm">pid {w.pid}</div>
      </td>
      <td>
        {w.status === "active" && <span className="pill pill-ended"><span className="dot"></span>active</span>}
        {w.status === "draining" && <span className="pill pill-paused"><span className="dot"></span>draining</span>}
        {w.status === "dead" && <span className="pill pill-failed"><span className="dot"></span>dead</span>}
      </td>
      <td>
        <CapacityBar inFlight={inFlight} capacity={capacity} />
        <div className="mono muted text-sm" style={{ marginTop: 2 }}>
          {inFlight} / {capacity}
          {onWorker.length > 0 && <> · <span style={{ color: "var(--blue)" }}>{onWorker.length} session{onWorker.length === 1 ? "" : "s"}</span></>}
        </div>
      </td>
      <td className={heartbeatBad ? "mono" : "mono muted"} style={{ color: heartbeatBad ? "var(--red)" : undefined }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{
            width: 6, height: 6, borderRadius: "50%",
            background: heartbeatBad ? "var(--red)" : "var(--green)",
            animation: "pulse 1.6s ease-in-out infinite",
          }}></span>
          {heartbeatAge.toFixed(1)}s ago
        </span>
      </td>
      <td className="mono muted">{startedAtSecondsAgo != null ? relativeTime(startedAtSecondsAgo) : "—"}</td>
      <td style={{ textAlign: "right", paddingRight: 12 }}>
        <Btn
          size="sm"
          kind="ghost"
          icon="alert"
          disabled={w.status !== "active" || draining}
          onClick={() => onDrain(w)}
        >
          {draining ? "Draining…" : "Drain"}
        </Btn>
      </td>
    </tr>
  );
}

function CapacityBar({ inFlight, capacity }) {
  const safe = Math.max(0, capacity | 0);
  const segments = Array.from({ length: safe });
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
              background: filled ? (safe > 0 && inFlight / safe > 0.8 ? "var(--amber)" : "var(--accent)") : "var(--bg-2)",
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
