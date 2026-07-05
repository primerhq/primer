/* global React, Icon */
//
// <SchemaPanel> — the collapsible structured-output side panel shell
// (Task B4 of the chat-refactor plan, R3). <Conversation>
// (ui/components/chat/conversation.jsx) mounts this as an optional
// right-hand sibling of the timeline + composer, gated by its
// `showSchemaPanel` prop (Task B2). Collapsed by default; a header
// toggle opens it.
//
// This ships the [Builder|JSON] tab strip + a placeholder body only —
// the actual Builder (flat fields + nested objects/arrays + basic
// scalar types) and the live-validated JSON editor land in Task F2.
// `value`/`onChange`/`valid`/`onValidityChange` are accepted now so
// the prop surface (and <Composer>'s `schemaInvalid` gate, which reads
// off this panel's validity) is stable ahead of that phase.

function SchemaPanel({
  value,
  onChange,
  persistent,
  onPersistentChange,
  valid,
  onValidityChange,
  collapsed = true,
  onToggle,
}) {
  const [tab, setTab] = React.useState("builder"); // "builder" | "json"

  if (collapsed) {
    return (
      <div
        className="schema-panel schema-panel-collapsed"
        style={{
          borderLeft: "1px solid var(--border)",
          display: "flex",
          alignItems: "flex-start",
          padding: "8px 4px",
        }}
      >
        <button
          type="button"
          title="Show structured output panel"
          data-testid="schema-panel-toggle"
          onClick={onToggle}
          style={{
            background: "transparent",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: "6px 4px",
            color: "var(--text-2)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
          }}
        >
          <Icon name="chevron-left" size={14} />
        </button>
      </div>
    );
  }

  return (
    <div
      className="schema-panel"
      style={{
        borderLeft: "1px solid var(--border)",
        width: 280,
        flexShrink: 0,
        display: "flex",
        flexDirection: "column",
        minWidth: 0,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "8px 10px",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <div className="tab-strip" style={{ display: "flex", gap: 4 }}>
          <button
            type="button"
            data-testid="schema-tab-builder"
            onClick={() => setTab("builder")}
            className={tab === "builder" ? "btn btn-sm btn-primary" : "btn btn-sm"}
          >Builder</button>
          <button
            type="button"
            data-testid="schema-tab-json"
            onClick={() => setTab("json")}
            className={tab === "json" ? "btn btn-sm btn-primary" : "btn btn-sm"}
          >JSON</button>
        </div>
        <button
          type="button"
          title="Collapse structured output panel"
          data-testid="schema-panel-toggle"
          onClick={onToggle}
          style={{ background: "transparent", border: "none", color: "var(--text-3)", cursor: "pointer" }}
        >
          <Icon name="chevron-right" size={14} />
        </button>
      </div>

      <div style={{ padding: "8px 10px", borderBottom: "1px solid var(--border)" }}>
        <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-2)" }}>
          <input
            type="checkbox"
            checked={!!persistent}
            onChange={(e) => typeof onPersistentChange === "function" && onPersistentChange(e.target.checked)}
          />
          Persistent
        </label>
        {valid === false && (
          <div style={{ marginTop: 6, fontSize: 11, color: "var(--red, #c33)" }}>
            <Icon name="warn-circle" size={11} /> schema invalid — send disabled
          </div>
        )}
      </div>

      <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 10, color: "var(--text-3)", fontSize: 12 }}>
        {tab === "builder" ? (
          <div data-testid="schema-builder-body">
            Schema Builder — coming soon (Task F2).
          </div>
        ) : (
          <textarea
            data-testid="schema-json-body"
            className="textarea"
            defaultValue={value ? JSON.stringify(value, null, 2) : ""}
            onChange={(e) => {
              const text = e.target.value;
              if (!text.trim()) {
                if (typeof onChange === "function") onChange(null);
                if (typeof onValidityChange === "function") onValidityChange(true);
                return;
              }
              try {
                const parsed = JSON.parse(text);
                if (typeof onChange === "function") onChange(parsed);
                if (typeof onValidityChange === "function") onValidityChange(true);
              } catch {
                if (typeof onValidityChange === "function") onValidityChange(false);
              }
            }}
            placeholder="{ }"
            style={{ width: "100%", minHeight: 160, resize: "vertical" }}
          />
        )}
      </div>
    </div>
  );
}

window.SchemaPanel = SchemaPanel;
