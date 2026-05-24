/* global React, Icon, Btn, Modal, relativeTime, fmtDate */

function WorkersPage({ workers, sessions, onPatchWorker, pushToast }) {
  const [drainTarget, setDrainTarget] = React.useState(null);
  const [, tick] = React.useState(0);
  // Tick heartbeats live
  React.useEffect(() => {
    const id = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const totals = workers.reduce(
    (acc, w) => {
      acc.cap += w.capacity;
      acc.flight += w.in_flight;
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
    onPatchWorker(w.id, { status: "draining" });
    pushToast({ kind: "warning", title: `Draining ${w.id}`, detail: `In-flight sessions on this worker will finish before drain completes. New sessions won't be claimed.` });
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      {/* Summary strip */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        <SummaryStat label="Total" value={workers.length} sub="registered workers" />
        <SummaryStat label="Active" value={totals.active} sub={`${totals.draining} draining`} accent={totals.active === 0 ? "red" : "green"} />
        <SummaryStat label="In flight" value={`${totals.flight} / ${totals.cap}`} sub="claim utilization" accent={totals.flight / totals.cap > 0.8 ? "amber" : "green"} />
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
              <WorkerRow key={w.id} w={w} sessions={sessions} onDrain={onDrain} />
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

function WorkerRow({ w, sessions, onDrain }) {
  const onWorker = sessions.filter((s) => s.worker_id === w.id && (s.status === "running" || s.status === "paused"));
  const heartbeatAge = w.heartbeat + Math.floor(Math.random() * 2);
  const heartbeatBad = heartbeatAge > 30;
  const utilization = w.in_flight / w.capacity;

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
        <CapacityBar inFlight={w.in_flight} capacity={w.capacity} />
        <div className="mono muted text-sm" style={{ marginTop: 2 }}>
          {w.in_flight} / {w.capacity}
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
      <td className="mono muted">{relativeTime(w.started_at)}</td>
      <td style={{ textAlign: "right", paddingRight: 12 }}>
        <Btn size="sm" kind="ghost" icon="alert" disabled={w.status !== "active"} onClick={() => onDrain(w)}>Drain</Btn>
      </td>
    </tr>
  );
}

function CapacityBar({ inFlight, capacity }) {
  const segments = Array.from({ length: capacity });
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
