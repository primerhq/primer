/* global React */

// channels-prompt mockup. Shows what an ask_user prompt looks like
// when delivered to Slack, Discord, or Telegram. Each platform's
// idiom is drawn approximately so doc readers recognise the surface.

function ChannelsPromptMockup({
  platform = "slack",
  question = "Approve plan v3?",
  options = ["Approve", "Reject"],
  agentName = "scheduled-summariser",
}) {
  if (platform === "discord") {
    return (
      <div style={{
        background: "#2f3136",
        color: "#dcddde",
        padding: "12px 16px",
        borderRadius: 6,
        borderLeft: "4px solid #5865f2",
        fontFamily: "var(--sans)",
        maxWidth: 460,
      }}>
        <div style={{ fontSize: 11, color: "#b9bbbe", marginBottom: 6 }}>
          {agentName} BOT &middot; today at 09:14
        </div>
        <div style={{ marginBottom: 10, fontSize: 14 }}>{question}</div>
        <div style={{ display: "flex", gap: 6 }}>
          {options.map((o) => (
            <span key={o} style={{
              background: "#4f545c",
              color: "#fff",
              padding: "4px 12px",
              borderRadius: 3,
              fontSize: 12,
            }}>{o}</span>
          ))}
        </div>
      </div>
    );
  }
  if (platform === "telegram") {
    return (
      <div style={{
        background: "#e7f4fc",
        padding: "10px 14px",
        borderRadius: 12,
        borderBottomLeftRadius: 4,
        fontFamily: "var(--sans)",
        maxWidth: 360,
        color: "#222",
      }}>
        <div style={{ fontSize: 12, color: "#3b78a3", fontWeight: 600, marginBottom: 4 }}>
          {agentName}
        </div>
        <div style={{ marginBottom: 8 }}>{question}</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {options.map((o) => (
            <span key={o} style={{
              background: "#fff",
              border: "1px solid #b3d6ec",
              padding: "6px 10px",
              borderRadius: 4,
              textAlign: "center",
              fontSize: 12,
              color: "#3b78a3",
            }}>{o}</span>
          ))}
        </div>
      </div>
    );
  }
  // Default: Slack.
  return (
    <div style={{
      background: "#fff",
      color: "#1d1c1d",
      padding: "10px 14px",
      borderRadius: 6,
      border: "1px solid #ddd",
      fontFamily: "var(--sans)",
      maxWidth: 460,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
        <span style={{ fontWeight: 700 }}>{agentName}</span>
        <span style={{
          background: "#e8e8e8", color: "#616061",
          fontSize: 10, padding: "1px 4px", borderRadius: 2, fontWeight: 600,
        }}>APP</span>
        <span style={{ color: "#616061", fontSize: 12 }}>9:14 AM</span>
      </div>
      <div style={{ marginBottom: 8, fontSize: 14 }}>{question}</div>
      <div style={{ display: "flex", gap: 6 }}>
        {options.map((o) => (
          <span key={o} style={{
            background: "#fff",
            border: "1px solid #b8b8b8",
            padding: "4px 12px",
            borderRadius: 4,
            fontSize: 12,
            color: "#1d1c1d",
          }}>{o}</span>
        ))}
      </div>
    </div>
  );
}

window.ChannelsPromptMockup = ChannelsPromptMockup;
