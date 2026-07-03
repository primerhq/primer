/* global React, Icon, Btn, Modal, relativeTime, fmtDate */

function WorkersPage({ pushToast }) {
  const { useResource, useMutation, useViewport, apiFetch } = window.primerApi;
  const { isMobile } = useViewport();
  const [drainTarget, setDrainTarget] = React.useState(null);
  const [clearDeadOpen, setClearDeadOpen] = React.useState(false);
  // Row-click detail: track the id (not the record) so the drawer
  // reflects the 2s poll + 1s heartbeat tick live instead of a stale
  // snapshot; it closes itself if the worker disappears.
  const [detailId, setDetailId] = React.useState(null);
  const [filterText, setFilterText] = React.useState("");
  // Status chip: "all" | "active" | "draining" | "dead".
  const [statusFilter, setStatusFilter] = React.useState("all");
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

  // Remove a single DEAD worker row (409 from the API for a non-dead
  // worker — the button only renders on dead rows, so that's a guard-rail).
  const deleteTargetRef = React.useRef(null);
  const deleteMut = useMutation(
    (id) => {
      deleteTargetRef.current = id;
      return apiFetch("DELETE", `/workers/${encodeURIComponent(id)}`);
    },
    {
      invalidates: ["workers:list"],
      onSuccess: () =>
        pushToast({
          kind: "success",
          title: `Removed ${deleteTargetRef.current || "worker"}`.trim(),
          detail: "The dead worker was cleared from the registry.",
        }),
      onError: (err) =>
        pushToast({
          kind: "error",
          title: err.title || "Remove failed",
          detail: err.detail,
          requestId: err.requestId,
        }),
    }
  );

  // Bulk-clear every DEAD worker in one call.
  const purgeMut = useMutation(
    () => apiFetch("POST", "/workers/purge_dead"),
    {
      invalidates: ["workers:list"],
      onSuccess: (data) =>
        pushToast({
          kind: "success",
          title: `Cleared ${data?.removed ?? 0} dead worker${data?.removed === 1 ? "" : "s"}`,
          detail: "Dead workers were removed from the registry.",
        }),
      onError: (err) =>
        pushToast({
          kind: "error",
          title: err.title || "Clear dead failed",
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
      if (w.status === "dead") acc.dead += 1;
      return acc;
    },
    { cap: 0, flight: 0, active: 0, draining: 0, dead: 0 }
  );

  // Client-side filter: status chip + free-text over id / host.
  const q = filterText.trim().toLowerCase();
  const filtered = workers.filter((w) => {
    if (statusFilter !== "all" && w.status !== statusFilter) return false;
    if (!q) return true;
    return (
      (w.id || "").toLowerCase().includes(q) ||
      (w.host || "").toLowerCase().includes(q)
    );
  });

  const detail = detailId ? workers.find((w) => w.id === detailId) || null : null;

  const chips = ["all", "active", "draining", "dead"];
  const chipCount = (c) =>
    c === "all" ? workers.length : workers.filter((w) => w.status === c).length;

  const onDrain = (w) => setDrainTarget(w);
  const confirmDrain = () => {
    const w = drainTarget;
    setDrainTarget(null);
    if (w) drainMut.mutate(w.id);
  };
  const onDelete = (w) => deleteMut.mutate(w.id);
  const confirmClearDead = () => {
    setClearDeadOpen(false);
    purgeMut.mutate();
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      {/* Summary strip */}
      <div className={`metric-grid ${isMobile ? "metric-grid-mobile" : ""}`} style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        <SummaryStat
          label="Total"
          value={workers.length}
          sub="registered workers"
          title="Every worker process the pool knows about, including ones currently draining or dead."
        />
        <SummaryStat
          label="Active"
          value={totals.active}
          sub={`${totals.draining} draining`}
          accent={totals.active === 0 ? "red" : "green"}
          title="Workers ready to accept new work. Draining workers finish their in-flight leases but won't pick up new ones."
        />
        <SummaryStat
          label="Running now"
          value={`${totals.flight} / ${totals.cap}`}
          sub={`${totals.flight} task${totals.flight === 1 ? "" : "s"} · ${totals.cap} parallel slot${totals.cap === 1 ? "" : "s"}`}
          accent={totals.cap > 0 && totals.flight / totals.cap > 0.8 ? "amber" : "green"}
          title={
            "Left number: how many leases (agent turns, graph runs, harness ops, "
            + "trigger fires) are being processed right now across all active workers.\n"
            + "Right number: total capacity — sum of every active worker's parallel slot "
            + "count.\n"
            + "When the left side hits the right side the pool is saturated and new work "
            + "queues until a slot frees up. Pre-saturation, idle slots are fine — it just "
            + "means nothing's currently due."
          }
        />
        <SummaryStat
          label="Dead"
          value={totals.dead}
          sub={totals.dead > 0 ? "clear to tidy the registry" : "none — registry clean"}
          accent={totals.dead > 0 ? "red" : "green"}
          title="Workers reaped after they stopped heart-beating. Their rows linger until removed — use the per-row remove button or Clear dead."
        />
      </div>

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter workers…"
            aria-label="Filter workers by id or host"
            data-testid="workers-filter"
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
          />
        </div>
        <div className="sep-v" />
        <div className="chip-group" role="tablist" aria-label="Filter by status">
          {chips.map((c) => (
            <span
              key={c}
              className={`chip${statusFilter === c ? " active" : ""}`}
              role="tab"
              tabIndex={0}
              aria-selected={statusFilter === c}
              data-testid={`workers-chip-${c}`}
              onClick={() => setStatusFilter(c)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setStatusFilter(c);
                }
              }}
            >
              {c}
              <span className="mono muted" style={{ fontSize: 10 }}>{chipCount(c)}</span>
            </span>
          ))}
        </div>
        {totals.dead > 0 && (
          <Btn
            size="sm"
            kind="ghost"
            icon="trash"
            data-testid="workers-clear-dead"
            disabled={purgeMut.loading}
            onClick={() => setClearDeadOpen(true)}
          >
            {purgeMut.loading ? "Clearing…" : `Clear dead (${totals.dead})`}
          </Btn>
        )}
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
              <th style={{ width: 90, textAlign: "right" }}></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((w) => (
              <WorkerRow
                key={w.id}
                w={w}
                onSelect={(worker) => setDetailId(worker.id)}
                selected={w.id === detailId}
                onDrain={onDrain}
                onDelete={onDelete}
                draining={drainMut.loading}
                deleting={deleteMut.loading}
              />
            ))}
          </tbody>
        </table>
        {filtered.length === 0 && (
          <div className="empty" style={{ padding: "18px 14px" }} data-testid="workers-empty">
            {workers.length === 0
              ? "No workers registered."
              : "No workers match the current filter."}
          </div>
        )}
        <div className="tbl-foot">
          <span className="tabular">
            {filtered.length === workers.length
              ? `${workers.length} worker${workers.length === 1 ? "" : "s"}`
              : `${filtered.length} of ${workers.length} workers`}
            {" · polling every 2s"}
          </span>
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

      {clearDeadOpen && (
        <Modal
          title={`Clear ${totals.dead} dead worker${totals.dead === 1 ? "" : "s"}?`}
          danger
          onClose={() => setClearDeadOpen(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setClearDeadOpen(false)}>Cancel</Btn>
              <Btn kind="danger" icon="trash" data-testid="workers-clear-dead-confirm" onClick={confirmClearDead}>Clear dead</Btn>
            </>
          }
        >
          Removing every worker currently in the <strong>dead</strong> state from
          the registry.
          <ul>
            <li>Dead workers stopped heart-beating and were reaped — they never come back on their own.</li>
            <li>Active and draining workers are left untouched.</li>
            <li>This only tidies the registry; it does not stop any running process.</li>
          </ul>
        </Modal>
      )}

      {detail && (
        <Modal
          title={`Worker ${detail.id}`}
          width={520}
          onClose={() => setDetailId(null)}
          footer={
            <>
              {detail.status === "active" && (
                <Btn kind="ghost" icon="alert" onClick={() => { setDetailId(null); onDrain(detail); }}>Drain</Btn>
              )}
              {detail.status === "dead" && (
                <Btn kind="danger" icon="trash" onClick={() => { setDetailId(null); onDelete(detail); }}>Remove</Btn>
              )}
              <Btn kind="ghost" onClick={() => setDetailId(null)}>Close</Btn>
            </>
          }
        >
          <WorkerDetail w={detail} />
        </Modal>
      )}
    </div>
  );
}

function WorkerDetail({ w }) {
  const capacity = w.capacity || 0;
  const inFlight = w.in_flight || 0;
  const heartbeatAge = w.heartbeat ?? 0;
  const heartbeatBad = heartbeatAge > 30;
  const hbAbs = w.last_heartbeat ? fmtDate(new Date(w.last_heartbeat)) : "—";
  const startedSecondsAgo = w.started_at
    ? Math.max(0, (Date.now() - new Date(w.started_at).getTime()) / 1000)
    : null;
  const startedAbs = w.started_at ? fmtDate(new Date(w.started_at)) : "—";
  return (
    <div data-testid="worker-detail" className="col" style={{ gap: 10 }}>
      <DetailRow label="ID"><span className="mono">{w.id}</span></DetailRow>
      <DetailRow label="Host / PID">
        <span className="mono">{w.host}</span>
        <span className="mono muted"> · pid {w.pid}</span>
      </DetailRow>
      <DetailRow label="Status">
        {w.status === "active" && <span className="pill pill-ended"><span className="dot"></span>active</span>}
        {w.status === "draining" && <span className="pill pill-paused"><span className="dot"></span>draining</span>}
        {w.status === "dead" && <span className="pill pill-failed"><span className="dot"></span>dead</span>}
      </DetailRow>
      <DetailRow label="Capacity">
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ width: 140 }}><CapacityBar inFlight={inFlight} capacity={capacity} /></div>
          <span className="mono muted text-sm">{inFlight} / {capacity} slots in use</span>
        </div>
      </DetailRow>
      <DetailRow label="Last heartbeat">
        <span className="mono" style={{ color: heartbeatBad ? "var(--red)" : undefined }}>{heartbeatAge.toFixed(1)}s ago</span>
        <span className="mono muted text-sm"> · {hbAbs}</span>
      </DetailRow>
      <DetailRow label="Started">
        <span className="mono">{startedSecondsAgo != null ? relativeTime(startedSecondsAgo) : "—"}</span>
        <span className="mono muted text-sm"> · {startedAbs}</span>
      </DetailRow>
      <div className="muted text-sm" style={{ marginTop: 2 }}>
        The scheduler records only these membership fields per worker — no
        per-worker counters or heartbeat history are stored, so there is no
        time-series to chart here.
      </div>
    </div>
  );
}

function DetailRow({ label, children }) {
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "baseline" }}>
      <div className="muted text-sm mono" style={{ width: 120, flex: "0 0 120px", textTransform: "uppercase", letterSpacing: "0.05em", fontSize: 10.5 }}>{label}</div>
      <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
    </div>
  );
}

function WorkerRow({ w, onSelect, selected, onDrain, onDelete, draining, deleting }) {
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
    <tr
      className={selected ? "selected" : undefined}
      style={{ cursor: "pointer" }}
      data-testid="worker-row"
      role="button"
      tabIndex={0}
      aria-label={`Worker ${w.id} details`}
      onClick={() => onSelect(w)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(w);
        }
      }}
    >
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
      <td style={{ textAlign: "right", paddingRight: 12 }} onClick={(e) => e.stopPropagation()}>
        {w.status === "dead" ? (
          <Btn
            size="sm"
            kind="ghost"
            icon="trash"
            data-testid="worker-delete"
            disabled={deleting}
            onClick={() => onDelete(w)}
          >
            {deleting ? "Removing…" : "Remove"}
          </Btn>
        ) : (
          <Btn
            size="sm"
            kind="ghost"
            icon="alert"
            disabled={w.status !== "active" || draining}
            onClick={() => onDrain(w)}
          >
            {draining ? "Draining…" : "Drain"}
          </Btn>
        )}
      </td>
    </tr>
  );
}

function CapacityBar({ inFlight, capacity }) {
  const safe = Math.max(0, capacity | 0);
  const segments = Array.from({ length: safe });
  return (
    <div className="cv-auto" style={{ display: "flex", gap: 2 }}>
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

function SummaryStat({ label, value, sub, accent, title }) {
  const color = accent === "red" ? "var(--red)" : accent === "amber" ? "var(--amber)" : accent === "green" ? "var(--green)" : undefined;
  return (
    <div className="panel" title={title}>
      <div className="panel-body" style={{ padding: "12px 14px" }}>
        <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>{label}</div>
        <div className="mono tabular" style={{ fontSize: 22, fontWeight: 600, marginTop: 4, letterSpacing: "-0.02em", color }}>{value}</div>
        <div className="muted text-sm" style={{ marginTop: 2 }}>{sub}</div>
      </div>
    </div>
  );
}

window.WorkersPage = WorkersPage;
