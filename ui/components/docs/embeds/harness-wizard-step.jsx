/* global React, Icon */

// harness-wizard-step mockup. A wizard frame with a four-step
// progress strip + the current step's form panel. Props pick which
// step is focused.

function HarnessWizardStepMockup({ step = 2 }) {
  const steps = ["Source", "Manifest", "Bindings", "Confirm"];
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 8,
      background: "var(--bg)",
      width: "100%",
      maxWidth: 560,
      margin: "0 auto",
    }}>
      <div style={{
        padding: "12px 16px",
        borderBottom: "1px solid var(--border)",
        fontWeight: 600,
        fontSize: 14,
      }}>
        Install harness
      </div>
      <div style={{
        display: "flex", gap: 8, padding: "10px 16px",
        borderBottom: "1px solid var(--border)",
        background: "var(--bg-2)",
      }}>
        {steps.map((s, i) => {
          const idx = i + 1;
          const isCurrent = idx === step;
          const isDone = idx < step;
          return (
            <div key={s} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
              <span style={{
                width: 18, height: 18,
                borderRadius: "50%",
                background: isCurrent ? "var(--accent)" : isDone ? "var(--green)" : "var(--bg)",
                border: isCurrent || isDone ? "none" : "1px solid var(--border)",
                color: isCurrent || isDone ? "#fff" : "var(--text-3)",
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                fontSize: 10, fontWeight: 600,
              }}>
                {isDone ? <Icon name="check" size={9} /> : idx}
              </span>
              <span style={{
                color: isCurrent ? "var(--text)" : "var(--text-3)",
                fontWeight: isCurrent ? 600 : 400,
              }}>{s}</span>
              {idx < steps.length && (
                <span style={{ color: "var(--text-3)", marginLeft: 4 }}>/</span>
              )}
            </div>
          );
        })}
      </div>
      <div style={{ padding: 16, fontSize: 12.5, minHeight: 160 }}>
        {step === 1 && (
          <div>
            <label className="muted text-sm">Git source URL</label>
            <input className="input" placeholder="https://github.com/org/harness-repo" />
            <label className="muted text-sm" style={{ marginTop: 10 }}>Branch or tag</label>
            <input className="input" placeholder="main" />
          </div>
        )}
        {step === 2 && (
          <div>
            <div className="muted text-sm" style={{ marginBottom: 8 }}>
              Detected manifest.yaml at the root. Two agents and
              one toolset will be installed:
            </div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 12, padding: 10, background: "var(--bg-2)", borderRadius: 4 }}>
              agents:<br />
              {"  - pr-reviewer"}<br />
              {"  - changelog-writer"}<br />
              toolsets:<br />
              {"  - gh"}
            </div>
          </div>
        )}
        {step === 3 && (
          <div className="muted text-sm">
            Bind harness toolsets to the harness agents (or to
            existing agents) before they can dispatch.
          </div>
        )}
        {step === 4 && (
          <div className="muted text-sm">
            Confirm install. Nothing is written until you click
            Install below.
          </div>
        )}
      </div>
      <div style={{
        display: "flex", justifyContent: "space-between",
        padding: "12px 16px", borderTop: "1px solid var(--border)",
      }}>
        <button className="btn" style={{ fontSize: 12 }}>Back</button>
        <button className="btn btn-primary" style={{ fontSize: 12 }}>
          {step === steps.length ? "Install" : "Next"}
        </button>
      </div>
    </div>
  );
}

window.HarnessWizardStepMockup = HarnessWizardStepMockup;
