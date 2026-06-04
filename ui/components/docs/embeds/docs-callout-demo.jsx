/* global React, Icon */

// docs-callout-demo mockup. Renders every callout kind stacked
// vertically with a one-line example body. Used by the authoring
// guide to demonstrate the available severities side by side.

function DocsCalloutDemoMockup() {
  const kinds = [
    { kind: "info",    color: "var(--blue)",   icon: "info",          body: "An informational note." },
    { kind: "success", color: "var(--green)",  icon: "check-circle",  body: "Action completed cleanly." },
    { kind: "warning", color: "var(--amber)",  icon: "alert",         body: "Watch out for this." },
    { kind: "danger",  color: "var(--red)",    icon: "x-circle",      body: "Stop and read carefully." },
    { kind: "tip",     color: "var(--violet)", icon: "zap",           body: "A handy shortcut." },
  ];
  return (
    <div>
      {kinds.map(({ kind, color, icon, body }) => (
        <div key={kind} style={{
          borderLeft: `3px solid ${color}`,
          background: "var(--bg-2)",
          padding: "8px 12px",
          margin: "6px 0",
          borderRadius: 4,
        }}>
          <div style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontWeight: 600,
            color,
            fontSize: 11,
            textTransform: "uppercase",
            letterSpacing: "0.04em",
            marginBottom: 4,
          }}>
            <Icon name={icon} size={12} />
            {kind}
          </div>
          <div style={{ fontSize: 13 }}>{body}</div>
        </div>
      ))}
    </div>
  );
}

window.DocsCalloutDemoMockup = DocsCalloutDemoMockup;
