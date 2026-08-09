/* global React */
// The navigator drawer: the active feature's list. Native sections for
// work (sessions grouped by workspace) and agents; every other group
// lists its classic pages from S2_LEGACY_ROUTES until it migrates.
// Keyboard: filter -> ArrowDown/j/k rows -> Enter opens; Esc blurs.

function S2_statusColor(status) {
  const s = String(status || "").toLowerCase();
  if (s === "running") return "var(--green)";
  if (s === "waiting") return "var(--amber)";
  if (s === "failed") return "var(--red)";
  return "var(--text-4)";
}

function S2_Nav({ nav }) {
  const { useResource, apiFetch } = window.primerApi;
  const [filter, setFilter] = React.useState("");
  const rowRefs = React.useRef([]);
  window.S2_Docs.useDocsVersion();

  const sessions = useResource(
    "studio2:sessions",
    (signal) => apiFetch("GET", "/sessions?limit=200", null, { signal }),
    { pollMs: 3000 },
  );
  const agents = useResource(
    "studio2:agents",
    (signal) => apiFetch("GET", "/agents?limit=200", null, { signal }),
    { pollMs: 10000 },
  );
  const sessionItems = (sessions.data && sessions.data.items) || [];
  const agentItems = (agents.data && agents.data.items) || [];

  const q = filter.trim().toLowerCase();
  const match = (s) => !q || String(s).toLowerCase().includes(q);
  const sections = [];
  if (nav === "work") {
    const byWs = {};
    for (const s of sessionItems) {
      if (!match(s.id)) continue;
      (byWs[s.workspace_id] = byWs[s.workspace_id] || []).push(s);
    }
    const ctxWs = window.S2_Ctx ? window.S2_Ctx.get() : null;
    const wsIds = Object.keys(byWs).sort(
      (a, b) => (a === ctxWs ? -1 : b === ctxWs ? 1 : a.localeCompare(b)),
    );
    for (const ws of wsIds) {
      sections.push({
        title: "sessions · " + ws,
        rows: byWs[ws].map((s) => {
          const isGraph = ((s.binding && s.binding.kind) || s.binding_kind) === "graph";
          return {
            key: "session:" + s.id,
            glyph: isGraph ? "◈" : "◆",
            label: s.id,
            status: s.status,
            open: () => window.S2_Docs.open("session", s.id),
          };
        }),
      });
    }
  } else if (nav === "agents") {
    sections.push({
      title: "agents",
      rows: agentItems.filter((a) => match(a.name || a.id)).map((a) => ({
        key: "agent:" + a.id, glyph: "◆", label: a.name || a.id,
        open: () => window.S2_Docs.open("agent", a.id),
      })),
    });
  }
  if (nav === "files") {
    const ctxWs = window.S2_Ctx ? window.S2_Ctx.get() : null;
    sections.push({
      title: "files",
      rows: ctxWs ? [{
        key: "legacy:/workspaces/" + ctxWs, glyph: "▢",
        label: "Workspace Studio (classic)",
        open: () => window.S2_Docs.open("legacy", "/workspaces/" + ctxWs),
      }] : [],
      emptyCopy: "Open a session first to set the workspace context.",
    });
  }
  const legacyRows = (window.S2_LEGACY_ROUTES || [])
    .filter((r) => r.group === nav && match(r.title))
    .map((r) => ({
      key: "legacy:" + r.ref, glyph: "▤", label: r.title + " (classic)",
      open: () => window.S2_Docs.open("legacy", r.ref),
    }));
  if (legacyRows.length) sections.push({ title: "classic pages", rows: legacyRows });

  const flatRows = sections.flatMap((s) => s.rows);
  rowRefs.current = [];
  const focusAt = (i) => {
    const els = rowRefs.current.filter(Boolean);
    if (els[i]) els[i].focus();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
      <input aria-label="Filter" placeholder="filter…" value={filter}
        data-testid="s2-nav-filter"
        onChange={(e) => setFilter(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); focusAt(0); }
          else if (e.key === "Escape") { e.preventDefault(); e.target.blur(); }
        }}
        style={{ margin: 8, padding: "5px 9px", background: "var(--bg)",
          border: "1px solid var(--border)", borderRadius: "var(--r-6)",
          color: "var(--text)", fontSize: "var(--fs-12)" }} />
      <div role="listbox" style={{ overflowY: "auto", padding: "0 4px 8px" }}>
        {sections.map((sec) => (
          <React.Fragment key={sec.title}>
            <div style={{ padding: "8px 8px 3px", fontSize: 10,
              letterSpacing: ".1em", textTransform: "uppercase",
              color: "var(--text-4)", fontWeight: 600 }}>{sec.title}</div>
            {sec.rows.map((row) => {
              const i = flatRows.indexOf(row);
              const isActive = window.S2_Docs.activeKey() === row.key;
              return (
                <button key={row.key} role="option" tabIndex={i === 0 ? 0 : -1}
                  ref={(el) => { rowRefs.current[i] = el; }}
                  aria-selected={isActive}
                  onClick={row.open}
                  onKeyDown={(e) => {
                    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); focusAt(Math.min(flatRows.length - 1, i + 1)); }
                    else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); if (i === 0) { const f = document.querySelector('[data-testid="s2-nav-filter"]'); if (f) f.focus(); } else focusAt(i - 1); }
                    else if (e.key === "Enter") { e.preventDefault(); row.open(); }
                  }}
                  style={{ display: "flex", gap: 8, width: "100%",
                    alignItems: "center", textAlign: "left", padding: "5px 8px",
                    cursor: "pointer", borderRadius: "var(--r-6)",
                    fontSize: "var(--fs-12)",
                    border: "1px solid " + (isActive ? "var(--border)" : "transparent"),
                    background: isActive ? "var(--bg-active)" : "none",
                    color: "var(--text-2)" }}>
                  {row.status !== undefined && (
                    <span style={{ width: 8, height: 8, borderRadius: "50%",
                      flex: "none", background: S2_statusColor(row.status) }} />
                  )}
                  <span style={{ color: "var(--text-3)", fontSize: 11,
                    flex: "none" }}>{row.glyph}</span>
                  <span className="mono" style={{ flex: 1, overflow: "hidden",
                    textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.label}</span>
                </button>
              );
            })}
            {!sec.rows.length && (
              <div style={{ padding: "10px 8px", color: "var(--text-4)",
                fontSize: "var(--fs-12)" }}>
                {sec.emptyCopy || "Nothing here yet in the trial."}
              </div>
            )}
          </React.Fragment>
        ))}
        {!sections.length && (
          <div style={{ padding: "14px 8px", color: "var(--text-4)",
            fontSize: "var(--fs-12)", textAlign: "center" }}>
            No entries match.
          </div>
        )}
      </div>
    </div>
  );
}
window.S2_Nav = S2_Nav;

// Quick-open noun index: agents + sessions from the live resource cache
// plus every legacy route. Later document kinds extend by wrapping items().
window.S2_QuickIndex = {
  items() {
    const out = [];
    const peek = (key) => {
      const api = window.primerApi._resource;
      const data = api && api.peekData ? api.peekData(key) : null;
      return (data && data.items) || [];
    };
    for (const a of peek("studio2:agents")) {
      out.push({ key: "agent:" + a.id, glyph: "◆", label: a.name || a.id,
        cat: "agent", go: () => window.S2_Docs.open("agent", a.id) });
    }
    for (const s of peek("studio2:sessions")) {
      out.push({ key: "session:" + s.id, glyph: "▣", label: s.id,
        cat: "session", go: () => window.S2_Docs.open("session", s.id) });
    }
    for (const r of (window.S2_LEGACY_ROUTES || [])) {
      out.push({ key: "legacy:" + r.ref, glyph: "▤", label: r.title,
        cat: "classic", go: () => window.S2_Docs.open("legacy", r.ref) });
    }
    return out;
  },
};
