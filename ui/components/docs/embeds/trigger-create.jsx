/* global React, Icon */

// trigger-create mockup. Modal form for the trigger create flow.
// Three kinds (cron / webhook / channel-pattern); the form's
// middle section swaps based on the selected kind.

function TriggerCreateMockup({ kind = "cron" }) {
  const kinds = ["cron", "webhook", "channel-pattern"];
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 8,
      background: "var(--bg)",
      width: "100%",
      maxWidth: 540,
      margin: "0 auto",
    }}>
      <div style={{
        padding: "12px 16px",
        borderBottom: "1px solid var(--border)",
        fontWeight: 600, fontSize: 14,
      }}>
        New trigger
      </div>
      <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <div>
          <label className="muted text-sm">Name</label>
          <input className="input" defaultValue="weekday-summary" />
        </div>
        <div>
          <label className="muted text-sm">Kind</label>
          <div style={{ display: "flex", gap: 6 }}>
            {kinds.map((k) => (
              <span key={k} style={{
                padding: "4px 10px",
                fontSize: 11,
                background: kind === k ? "var(--accent)" : "var(--bg-2)",
                color: kind === k ? "#fff" : "var(--text-3)",
                borderRadius: 4,
                fontFamily: "var(--mono)",
              }}>{k}</span>
            ))}
          </div>
        </div>
        {kind === "cron" && (
          <div>
            <label className="muted text-sm">Cron expression (UTC)</label>
            <input className="input" defaultValue="0 5 * * 1-5" style={{ fontFamily: "var(--mono)" }} />
            <div className="muted text-sm" style={{ marginTop: 4 }}>
              Fires at 05:00 UTC weekdays.
            </div>
          </div>
        )}
        {kind === "webhook" && (
          <div>
            <label className="muted text-sm">Webhook secret</label>
            <input className="input" defaultValue="(auto-generated on save)" disabled />
            <div className="muted text-sm" style={{ marginTop: 4 }}>
              POST endpoint shown on the trigger detail page.
            </div>
          </div>
        )}
        {kind === "channel-pattern" && (
          <div>
            <label className="muted text-sm">Channel</label>
            <select className="select">
              <option>ops-slack</option>
            </select>
            <label className="muted text-sm" style={{ marginTop: 8 }}>Regex</label>
            <input className="input" defaultValue="/deploy" style={{ fontFamily: "var(--mono)" }} />
          </div>
        )}
        <div>
          <label className="muted text-sm">Subscription target</label>
          <select className="select">
            <option>start_session (agent: weekly-digest)</option>
            <option>post_to_chat</option>
            <option>resume_yield</option>
          </select>
        </div>
      </div>
      <div style={{
        display: "flex", justifyContent: "flex-end", gap: 8,
        padding: "12px 16px", borderTop: "1px solid var(--border)",
      }}>
        <button className="btn" style={{ fontSize: 12 }}>Cancel</button>
        <button className="btn btn-primary" style={{ fontSize: 12 }}>Create trigger</button>
      </div>
    </div>
  );
}

window.TriggerCreateMockup = TriggerCreateMockup;
