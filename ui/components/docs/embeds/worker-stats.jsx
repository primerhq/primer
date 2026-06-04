/* global React, Icon */

// worker-stats mockup. Compact dashboard tile showing pool
// utilisation, lease counts, and a small sparkline placeholder.

function WorkerStatsMockup({
  total = 8,
  busy = 5,
  parked = 2,
  failed = 0,
}) {
  const idle = Math.max(0, total - busy);
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 6,
      background: "var(--bg)",
      padding: 14,
      width: "100%",
      maxWidth: 420,
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        marginBottom: 12,
      }}>
        <div style={{ fontWeight: 600, fontSize: 13 }}>Workers</div>
        <span className="muted text-sm" style={{ marginLeft: "auto" }}>pool size {total}</span>
      </div>
      <div style={{
        display: "flex", height: 18, borderRadius: 4, overflow: "hidden",
        background: "var(--bg-2)",
      }}>
        <div style={{
          flexBasis: `${(busy / total) * 100}%`,
          background: "var(--green)",
        }} />
        <div style={{
          flexBasis: `${(parked / total) * 100}%`,
          background: "var(--amber)",
        }} />
        <div style={{
          flexBasis: `${(failed / total) * 100}%`,
          background: "var(--red)",
        }} />
      </div>
      <div style={{
        display: "grid",
        gridTemplateColumns: "1fr 1fr 1fr 1fr",
        gap: 8,
        marginTop: 10,
        fontSize: 11.5,
      }}>
        <div>
          <div style={{ color: "var(--green)", fontWeight: 600 }}>{busy}</div>
          <div className="muted">busy</div>
        </div>
        <div>
          <div style={{ color: "var(--amber)", fontWeight: 600 }}>{parked}</div>
          <div className="muted">parked</div>
        </div>
        <div>
          <div style={{ color: "var(--text-2)", fontWeight: 600 }}>{idle}</div>
          <div className="muted">idle</div>
        </div>
        <div>
          <div style={{ color: failed ? "var(--red)" : "var(--text-3)", fontWeight: 600 }}>{failed}</div>
          <div className="muted">failed</div>
        </div>
      </div>
    </div>
  );
}

window.WorkerStatsMockup = WorkerStatsMockup;
