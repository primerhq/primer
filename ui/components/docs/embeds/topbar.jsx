/* global React, Icon */

// Topbar mockup. Visually matches the production topbar with hardcoded
// data (no API calls). Props customise the indicators.

function TopbarMockup({
  workers = "5/8",
  inFlight = "2 in flight",
  showIcBell = false,
  showThemeToggle = true,
  username = "alex",
}) {
  return (
    <div style={{
      display: "flex",
      alignItems: "center",
      padding: "0 16px",
      height: 48,
      background: "var(--bg)",
      borderBottom: "1px solid var(--border)",
      fontSize: 12,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <div style={{
          width: 22, height: 22, background: "var(--accent)",
          borderRadius: 4, opacity: 0.4,
        }}/>
        <span style={{ fontWeight: 600 }}>primer</span>
        <span className="muted text-sm">localhost:8000</span>
      </div>
      <div style={{
        marginLeft: 24, flex: 1, display: "flex", alignItems: "center",
        background: "var(--bg-2)", borderRadius: 4, padding: "4px 8px",
        maxWidth: 360, color: "var(--text-3)",
      }}>
        <Icon name="search" size={13} />
        <span style={{ marginLeft: 8 }}>Search...</span>
        <kbd style={{ marginLeft: "auto", fontSize: 10 }}>Cmd K</kbd>
      </div>
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          padding: "2px 8px", background: "var(--bg-2)",
          borderRadius: 12, fontSize: 11,
        }}>
          <span style={{
            width: 6, height: 6, borderRadius: "50%",
            background: "var(--green)",
          }}/>
          {workers} workers <span className="muted">{inFlight}</span>
        </span>
        {showIcBell && (
          <button style={{
            background: "none", border: "1px solid var(--amber)",
            borderRadius: 4, padding: 4, color: "var(--amber)", cursor: "pointer",
          }}>
            <Icon name="bell" size={13} />
          </button>
        )}
        {showThemeToggle && (
          <button style={{
            background: "none", border: "none", padding: 4,
            color: "var(--text-3)", cursor: "pointer",
          }}>
            <Icon name="moon" size={13} />
          </button>
        )}
        <div style={{
          width: 26, height: 26, borderRadius: "50%",
          background: "var(--bg-2)", display: "flex",
          alignItems: "center", justifyContent: "center",
          fontSize: 11, fontWeight: 600,
        }}>
          {username[0].toUpperCase()}
        </div>
      </div>
    </div>
  );
}

window.TopbarMockup = TopbarMockup;
