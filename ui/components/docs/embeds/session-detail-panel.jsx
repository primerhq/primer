/* global React, Icon */

// session-detail-panel mockup. Visually matches the session detail
// page: header strip + transcript pane + parked-status footer.

function SessionDetailPanelMockup({
  sessionId = "sess-a1b2c3",
  agentId = "weekly-digest",
  status = "running",
  turnCount = 4,
  parkedReason = null,
}) {
  const statusColor = {
    running: "var(--green)",
    parked: "var(--amber)",
    done: "var(--text-3)",
    failed: "var(--red)",
  }[status] || "var(--text-3)";
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 6,
      background: "var(--bg)",
      minHeight: 280,
      display: "flex",
      flexDirection: "column",
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "10px 14px",
        borderBottom: "1px solid var(--border)",
      }}>
        <code style={{ fontSize: 12, color: "var(--text-2)" }}>{sessionId}</code>
        <span className="muted text-sm">agent={agentId}</span>
        <span style={{
          marginLeft: "auto",
          display: "inline-flex", alignItems: "center", gap: 4,
          padding: "2px 8px",
          background: "var(--bg-2)",
          borderRadius: 12,
          fontSize: 11,
          color: statusColor,
        }}>
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: statusColor }} />
          {status}
        </span>
      </div>
      <div style={{ padding: 12, flex: 1, fontSize: 12.5, color: "var(--text-2)" }}>
        <div style={{ marginBottom: 8 }}>
          <strong>Turn 1</strong> &middot; user
          <div className="muted text-sm" style={{ marginLeft: 12 }}>
            Read yesterday's logs and summarise the top three issues.
          </div>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>Turn 2</strong> &middot; assistant
          <div className="muted text-sm" style={{ marginLeft: 12 }}>
            Calling read_workspace_file to list /var/log entries...
          </div>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>Turn 3</strong> &middot; tool result
          <div className="muted text-sm" style={{ marginLeft: 12, fontFamily: "var(--mono)" }}>
            [12 files; total 8.4 MB]
          </div>
        </div>
        <div style={{ marginBottom: 8 }}>
          <strong>Turn {turnCount}</strong> &middot; assistant
          <div className="muted text-sm" style={{ marginLeft: 12 }}>
            Streaming reply...
          </div>
        </div>
      </div>
      {parkedReason && (
        <div style={{
          padding: "8px 14px",
          borderTop: "1px solid var(--border)",
          background: "var(--bg-2)",
          fontSize: 11.5,
          color: "var(--amber)",
        }}>
          <Icon name="pause" size={11} style={{ marginRight: 6 }} />
          parked on: <code>{parkedReason}</code>
        </div>
      )}
    </div>
  );
}

window.SessionDetailPanelMockup = SessionDetailPanelMockup;
