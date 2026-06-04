/* global React, Icon */

// bug-reporter-modal mockup. Floating modal launched by the
// in-UI bug-reporter button: free-text description + auto-attached
// screenshot + page URL.

function BugReporterModalMockup({
  pageUrl = "/console/#/sessions/sess-a1b2",
  hasScreenshot = true,
}) {
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 8,
      background: "var(--bg)",
      width: "100%",
      maxWidth: 520,
      margin: "0 auto",
      boxShadow: "0 6px 24px rgba(0,0,0,0.08)",
    }}>
      <div style={{
        padding: "12px 16px",
        borderBottom: "1px solid var(--border)",
        fontWeight: 600, fontSize: 14,
        display: "flex", alignItems: "center", gap: 8,
      }}>
        <Icon name="alert" size={14} style={{ color: "var(--amber)" }} />
        Report a bug
      </div>
      <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <div>
          <label className="muted text-sm">What went wrong?</label>
          <textarea
            className="input"
            style={{ minHeight: 110 }}
            placeholder="The graph designer hangs when I drag a node onto..."
          />
        </div>
        <div style={{
          padding: "8px 12px",
          background: "var(--bg-2)",
          borderRadius: 4,
          display: "flex",
          gap: 16,
          fontSize: 12,
          color: "var(--text-2)",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <Icon name="image" size={12} className="muted" />
            screenshot {hasScreenshot ? "attached" : "skipped"}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontFamily: "var(--mono)" }}>
            <Icon name="link" size={12} className="muted" />
            {pageUrl}
          </div>
        </div>
      </div>
      <div style={{
        display: "flex", justifyContent: "flex-end", gap: 8,
        padding: "12px 16px", borderTop: "1px solid var(--border)",
      }}>
        <button className="btn" style={{ fontSize: 12 }}>Cancel</button>
        <button className="btn btn-primary" style={{ fontSize: 12 }}>Send report</button>
      </div>
    </div>
  );
}

window.BugReporterModalMockup = BugReporterModalMockup;
