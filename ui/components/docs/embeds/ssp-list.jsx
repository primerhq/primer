/* global React, Icon */

// ssp-list mockup. Semantic-search providers list with one active
// row and a few inactive rows. Visually mirrors the real
// /providers/semantic-search page.

function SspListMockup({
  activeId = "voyage-3-large",
}) {
  const rows = [
    { id: "voyage-3-large", model: "voyage-3-large", dim: 1024, kind: "voyage" },
    { id: "openai-text-embedding-3", model: "text-embedding-3-large", dim: 3072, kind: "openai" },
    { id: "local-bge", model: "BAAI/bge-small-en-v1.5", dim: 384, kind: "huggingface" },
  ];
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 6,
      background: "var(--bg)",
      padding: 12,
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        marginBottom: 10,
      }}>
        <div style={{ fontWeight: 600, fontSize: 13 }}>Semantic search providers</div>
        <button className="btn btn-primary" style={{ marginLeft: "auto", fontSize: 12 }}>
          <Icon name="plus" size={11} style={{ marginRight: 4 }} />
          Add provider
        </button>
      </div>
      <div style={{
        display: "grid",
        gridTemplateColumns: "1fr 1fr 80px 100px",
        fontSize: 11.5,
        gap: 8,
        padding: "6px 8px",
        color: "var(--text-3)",
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        borderBottom: "1px solid var(--border)",
      }}>
        <div>id</div>
        <div>model</div>
        <div>dim</div>
        <div>active</div>
      </div>
      {rows.map((r) => (
        <div key={r.id} style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 80px 100px",
          gap: 8,
          padding: "10px 8px",
          fontSize: 12.5,
          borderBottom: "1px solid var(--border)",
          alignItems: "center",
        }}>
          <code>{r.id}</code>
          <span className="muted">{r.model}</span>
          <span>{r.dim}</span>
          <span style={{
            display: "inline-flex", alignItems: "center", gap: 4,
            fontSize: 11,
            color: r.id === activeId ? "var(--green)" : "var(--text-3)",
          }}>
            <span style={{
              width: 6, height: 6, borderRadius: "50%",
              background: r.id === activeId ? "var(--green)" : "var(--text-3)",
            }} />
            {r.id === activeId ? "active" : "idle"}
          </span>
        </div>
      ))}
    </div>
  );
}

window.SspListMockup = SspListMockup;
