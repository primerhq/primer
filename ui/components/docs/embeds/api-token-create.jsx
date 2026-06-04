/* global React, Icon */

// api-token-create mockup. Modal for minting a new API token.
// After save the modal shows the token value once with a Copy
// button; the operator never sees it again.

function ApiTokenCreateMockup({
  phase = "form",
  tokenValue = "primer_at_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6",
}) {
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 8,
      background: "var(--bg)",
      width: "100%",
      maxWidth: 500,
      margin: "0 auto",
    }}>
      <div style={{
        padding: "12px 16px",
        borderBottom: "1px solid var(--border)",
        fontWeight: 600, fontSize: 14,
      }}>
        {phase === "form" ? "New API token" : "Save your token now"}
      </div>
      {phase === "form" && (
        <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <label className="muted text-sm">Label</label>
            <input className="input" defaultValue="ci-deploy-bot" />
          </div>
          <div>
            <label className="muted text-sm">Expires in (days)</label>
            <input className="input" type="number" defaultValue={90} />
          </div>
          <div>
            <label className="muted text-sm">Scopes</label>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {["agents", "sessions", "chats", "triggers"].map((s) => (
                <span key={s} className="pill" style={{ fontSize: 10 }}>{s}</span>
              ))}
            </div>
          </div>
        </div>
      )}
      {phase === "reveal" && (
        <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="muted text-sm">
            Copy this token now. It will not be shown again.
          </div>
          <div style={{
            padding: "10px 12px",
            background: "var(--bg-2)",
            borderRadius: 4,
            fontFamily: "var(--mono)",
            fontSize: 12,
            display: "flex",
            alignItems: "center",
            gap: 8,
            overflow: "hidden",
          }}>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
              {tokenValue}
            </span>
            <button className="btn" style={{ fontSize: 11, marginLeft: "auto" }}>
              <Icon name="copy" size={11} style={{ marginRight: 4 }} />Copy
            </button>
          </div>
        </div>
      )}
      <div style={{
        display: "flex", justifyContent: "flex-end", gap: 8,
        padding: "12px 16px", borderTop: "1px solid var(--border)",
      }}>
        {phase === "form" && <button className="btn" style={{ fontSize: 12 }}>Cancel</button>}
        <button className="btn btn-primary" style={{ fontSize: 12 }}>
          {phase === "form" ? "Mint token" : "Done"}
        </button>
      </div>
    </div>
  );
}

window.ApiTokenCreateMockup = ApiTokenCreateMockup;
