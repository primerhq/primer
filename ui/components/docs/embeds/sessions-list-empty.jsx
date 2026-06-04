/* global React, Icon */

// sessions-list-empty mockup. Visually matches the real /sessions
// empty state: filter bar + boxed empty message + primary action.

function SessionsListEmptyMockup({
  emptyLine = "No sessions yet",
  ctaLabel = "New session",
}) {
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 6,
      background: "var(--bg)",
      padding: 16,
      minHeight: 200,
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        marginBottom: 16,
      }}>
        <input
          className="input"
          placeholder="Filter sessions..."
          disabled
          style={{ flex: 1, fontSize: 12, background: "var(--bg-2)" }}
        />
        <select className="select" disabled style={{ fontSize: 12 }}>
          <option>any status</option>
        </select>
        <button className="btn btn-primary" style={{ fontSize: 12 }}>
          <Icon name="plus" size={11} style={{ marginRight: 4 }} />
          {ctaLabel}
        </button>
      </div>
      <div style={{
        display: "flex", flexDirection: "column", alignItems: "center",
        justifyContent: "center", padding: "40px 0", gap: 8,
        color: "var(--text-3)",
      }}>
        <Icon name="zap" size={28} className="muted" />
        <div style={{ fontWeight: 600, fontSize: 14, color: "var(--text-2)" }}>
          {emptyLine}
        </div>
        <div className="muted text-sm">
          Hit {ctaLabel} above to start one.
        </div>
      </div>
    </div>
  );
}

window.SessionsListEmptyMockup = SessionsListEmptyMockup;
