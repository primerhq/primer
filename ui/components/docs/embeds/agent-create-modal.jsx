/* global React, Icon */

// agent-create-modal mockup. Modal frame with a tab strip and a
// static form layout per tab. Props pick which tab is shown by
// default.

function AgentCreateModalMockup({
  tab = "basic",
  selectedToolset = "system",
}) {
  const tabs = ["basic", "tools", "prompt"];
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 8,
      background: "var(--bg)",
      width: "100%",
      maxWidth: 560,
      margin: "0 auto",
      boxShadow: "0 4px 24px rgba(0,0,0,0.08)",
    }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "12px 16px", borderBottom: "1px solid var(--border)",
      }}>
        <div style={{ fontWeight: 600, fontSize: 14 }}>Create agent</div>
        <Icon name="x" size={14} className="muted" />
      </div>
      <div style={{
        display: "flex", gap: 12, padding: "0 16px",
        borderBottom: "1px solid var(--border)",
      }}>
        {tabs.map((t) => (
          <div key={t} style={{
            padding: "10px 0",
            fontSize: 12,
            color: tab === t ? "var(--accent)" : "var(--text-3)",
            borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
            fontWeight: tab === t ? 600 : 400,
            textTransform: "capitalize",
          }}>
            {t}
          </div>
        ))}
      </div>
      <div style={{ padding: 16, fontSize: 12, minHeight: 180 }}>
        {tab === "basic" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <label className="muted text-sm">Name</label>
            <input className="input" placeholder="my-agent" />
            <label className="muted text-sm">Description</label>
            <input className="input" placeholder="What does this agent do?" />
            <label className="muted text-sm">Model</label>
            <select className="select"><option>claude-opus-4-8</option></select>
          </div>
        )}
        {tab === "tools" && (
          <div>
            <div className="muted text-sm" style={{ marginBottom: 8 }}>
              Toolsets bound to this agent:
            </div>
            {["system", "web", "workspaces", "misc"].map((ts) => (
              <div key={ts} style={{
                display: "flex", alignItems: "center", gap: 8,
                padding: "6px 0",
                borderBottom: "1px solid var(--border)",
              }}>
                <input
                  type="checkbox"
                  checked={ts === selectedToolset}
                  readOnly
                />
                <code>{ts}</code>
                <span className="muted text-sm" style={{ marginLeft: "auto" }}>
                  built-in
                </span>
              </div>
            ))}
          </div>
        )}
        {tab === "prompt" && (
          <div>
            <label className="muted text-sm">System prompt</label>
            <textarea
              className="input"
              style={{ width: "100%", minHeight: 120, fontFamily: "var(--mono)" }}
              placeholder="You are a helpful agent that..."
            />
          </div>
        )}
      </div>
      <div style={{
        display: "flex", justifyContent: "flex-end", gap: 8,
        padding: "12px 16px", borderTop: "1px solid var(--border)",
      }}>
        <button className="btn" style={{ fontSize: 12 }}>Cancel</button>
        <button className="btn btn-primary" style={{ fontSize: 12 }}>Create</button>
      </div>
    </div>
  );
}

window.AgentCreateModalMockup = AgentCreateModalMockup;
