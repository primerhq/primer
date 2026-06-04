/* global React, Icon */

// workspace-empty mockup. Visually matches the real /workspaces
// empty state: provider strip + boxed empty message + primary
// 'Create workspace' action.

function WorkspaceEmptyMockup({
  emptyLine = "No workspaces yet",
  ctaLabel = "Create workspace",
  providerName = "local",
}) {
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 6,
      background: "var(--bg)",
      padding: 16,
      minHeight: 220,
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        marginBottom: 16,
      }}>
        <div style={{
          padding: "4px 10px",
          background: "var(--bg-2)",
          borderRadius: 12,
          fontSize: 11,
          color: "var(--text-2)",
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
        }}>
          <Icon name="circle" size={9} className="muted" />
          provider: <code>{providerName}</code>
        </div>
        <button className="btn btn-primary" style={{ fontSize: 12, marginLeft: "auto" }}>
          <Icon name="plus" size={11} style={{ marginRight: 4 }} />
          {ctaLabel}
        </button>
      </div>
      <div style={{
        display: "flex", flexDirection: "column", alignItems: "center",
        justifyContent: "center", padding: "40px 0", gap: 8,
        color: "var(--text-3)",
      }}>
        <Icon name="folder" size={28} className="muted" />
        <div style={{ fontWeight: 600, fontSize: 14, color: "var(--text-2)" }}>
          {emptyLine}
        </div>
        <div className="muted text-sm">
          Workspaces are sandboxed filesystems where sessions run.
          Pick a template, hit {ctaLabel}.
        </div>
      </div>
    </div>
  );
}

window.WorkspaceEmptyMockup = WorkspaceEmptyMockup;
