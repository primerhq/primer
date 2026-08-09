/* global React */
// The center tab bar + the active-document host.
function S2_TabBar() {
  window.S2_Docs.useDocsVersion();
  const tabs = window.S2_Docs.tabs();
  const active = window.S2_Docs.activeKey();
  if (!tabs.length) return null;
  return (
    <div role="tablist" data-testid="s2-tabbar"
      style={{ display: "flex", alignItems: "flex-end", height: 35,
        background: "var(--bg-1)", borderBottom: "1px solid var(--border)",
        overflowX: "auto", flex: "none" }}>
      {tabs.map((t) => (
        <button key={t.key} role="tab" aria-selected={t.key === active}
          onClick={() => window.S2_Docs.activate(t.key)}
          style={{ display: "flex", gap: 7, alignItems: "center",
            padding: "7px 11px", fontSize: "var(--fs-12)", cursor: "pointer",
            border: "1px solid " + (t.key === active ? "var(--border)" : "transparent"),
            borderBottom: "none", background: t.key === active ? "var(--bg)" : "none",
            color: t.key === active ? "var(--text)" : "var(--text-3)",
            borderRadius: "var(--r-6) var(--r-6) 0 0", whiteSpace: "nowrap",
            maxWidth: 220 }}>
          <span style={{ fontSize: 11, color: "var(--text-3)" }}>{t.glyph}</span>
          <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{t.title}</span>
          {t.dirty && <span title="unsaved changes"
            style={{ width: 7, height: 7, borderRadius: "50%",
              background: "var(--amber)", flex: "none" }} />}
          <span role="button" aria-label={"Close " + t.title} tabIndex={-1}
            onClick={(e) => { e.stopPropagation(); window.S2_Docs.close(t.key); }}
            style={{ color: "var(--text-4)", flex: "none" }}>×</span>
        </button>
      ))}
    </div>
  );
}
window.S2_TabBar = S2_TabBar;

function S2_ActiveDoc() {
  window.S2_Docs.useDocsVersion();
  const key = window.S2_Docs.activeKey();
  if (!key) {
    const mod = window.S2_MOD || "Ctrl";
    return (
      <div style={{ display: "grid", placeItems: "center", flex: 1,
        color: "var(--text-3)" }}>
        <div style={{ textAlign: "center", display: "grid", gap: 9 }}>
          <b style={{ color: "var(--text-2)", fontWeight: 600 }}>Primer Studio (trial)</b>
          <div style={{ fontSize: "var(--fs-12)", display: "grid", gap: 6 }}>
            <span><span className="s2-kbd">{mod} K</span> commands</span>
            <span><span className="s2-kbd">{mod} P</span> open anything</span>
            <span><span className="s2-kbd">g</span> then a letter: navigators</span>
          </div>
        </div>
      </div>
    );
  }
  const tab = window.S2_Docs.tabs().find((t) => t.key === key);
  const def = window.S2_Docs.kindDef(tab.kind);
  if (!def) {
    return (
      <div style={{ display: "grid", placeItems: "center", flex: 1,
        color: "var(--text-3)", fontSize: "var(--fs-12)" }}>
        This document kind is not available in the trial yet.
      </div>
    );
  }
  const docApi = {
    setDirty: (b) => window.S2_Docs.setDirty(key, b),
    close: () => window.S2_Docs.close(key),
  };
  return (
    <div style={{ flex: 1, minHeight: 0, overflowY: "auto",
      display: "flex", flexDirection: "column" }} data-testid="s2-doc">
      {def.render(tab.ref, docApi)}
    </div>
  );
}
window.S2_ActiveDoc = S2_ActiveDoc;
