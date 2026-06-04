/* global React, Icon */

// workspace-template-form mockup. Modal form for creating a
// workspace template: name, provider, base image, env vars, TTL.

function WorkspaceTemplateFormMockup({
  templateName = "python-3.13-default",
  providerKind = "local",
  baseImage = "python:3.13-slim",
}) {
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
        New workspace template
      </div>
      <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <div>
          <label className="muted text-sm">Name</label>
          <input className="input" defaultValue={templateName} />
        </div>
        <div>
          <label className="muted text-sm">Provider</label>
          <select className="select" defaultValue={providerKind}>
            <option value="local">local</option>
            <option value="docker">docker</option>
            <option value="kubernetes">kubernetes</option>
          </select>
        </div>
        <div>
          <label className="muted text-sm">Base image</label>
          <input className="input" defaultValue={baseImage} />
        </div>
        <div>
          <label className="muted text-sm">TTL (minutes)</label>
          <input className="input" type="number" defaultValue={30} />
        </div>
        <div>
          <label className="muted text-sm">Environment variables (one per line)</label>
          <textarea
            className="input"
            style={{ fontFamily: "var(--mono)", minHeight: 56 }}
            defaultValue={"PYTHONUNBUFFERED=1\nNODE_ENV=development"}
          />
        </div>
      </div>
      <div style={{
        display: "flex", justifyContent: "flex-end", gap: 8,
        padding: "12px 16px", borderTop: "1px solid var(--border)",
      }}>
        <button className="btn" style={{ fontSize: 12 }}>Cancel</button>
        <button className="btn btn-primary" style={{ fontSize: 12 }}>Create template</button>
      </div>
    </div>
  );
}

window.WorkspaceTemplateFormMockup = WorkspaceTemplateFormMockup;
