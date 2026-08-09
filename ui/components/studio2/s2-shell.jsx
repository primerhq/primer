/* global React */
// Studio2 - trial shell for the consolidated console.
// Spec: docs/superpowers/specs/2026-08-08-studio-consolidation-design.md
// Design bar: docs/superpowers/specs/2026-08-09-studio-design-pack-analysis.md
// Renders INSTEAD of <App/> when the hash path is /studio2 (see the
// S2_RootGate in app.jsx). The classic console is untouched otherwise.

const S2_STYLE = `
  .s2-root{position:fixed;inset:0;display:grid;grid-template-rows:40px 1fr 26px;
    background:var(--bg);color:var(--text);font-size:var(--fs-13);z-index:50}
  .s2-mid{display:grid;grid-template-columns:44px 232px 1fr 272px;min-height:0}
  .s2-menubar{display:flex;align-items:center;gap:var(--sp-2);
    background:var(--bg-1);border-bottom:1px solid var(--border);padding:0 var(--sp-2)}
  .s2-rail{background:var(--bg-1);border-right:1px solid var(--border)}
  .s2-nav{background:var(--bg-1);border-right:1px solid var(--border);
    overflow-y:auto;min-height:0}
  .s2-center{display:flex;flex-direction:column;min-width:0;min-height:0}
  .s2-right{background:var(--bg-1);border-left:1px solid var(--border);
    overflow-y:auto;min-height:0}
  .s2-term{border-top:1px solid var(--border);background:var(--bg-1)}
  .s2-status{display:flex;align-items:center;gap:var(--sp-3);
    background:var(--bg-1);border-top:1px solid var(--border);
    padding:0 var(--sp-3);font-size:var(--fs-11);color:var(--text-3);
    font-family:var(--font-mono);font-variant-numeric:tabular-nums}
  /* Design-pack rule 12: a visible ring on every focusable thing. */
  .s2-root :focus-visible{outline:2px solid var(--accent);outline-offset:-1px;
    border-radius:4px}
  /* Design-pack rule 13: shortcuts render as kbd chips. */
  .s2-kbd{font-family:var(--font-mono);font-size:var(--fs-11);
    color:var(--text-3);background:var(--bg-2);border:1px solid var(--border);
    border-radius:4px;padding:1px 5px;white-space:nowrap}
  .s2-gate{position:fixed;inset:0;display:none;place-items:center;
    background:var(--bg);z-index:60;text-align:center;padding:var(--sp-4)}
  @media (max-width: 900px){ .s2-gate{display:grid} }
`;

window.S2_TRIAL_BADGE_TEXT = "studio trial";

// The classic chrome owns the toast viewport; the trial shell replaces
// the chrome, so it mounts its own viewport over the SAME shared store
// (window.primerApi.useToast) - pushes from reused components land here.
function S2_Toasts() {
  const { toasts, dismiss } = window.primerApi.useToast();
  return (
    <div style={{ position: "fixed", right: 18, bottom: 34,
      display: "grid", gap: 8, zIndex: 120 }}>
      {toasts.map((t) => (
        <div key={t.id} role="status" data-testid="s2-toast"
          onClick={() => dismiss(t.id)}
          style={{ background: "var(--bg-elev)",
            border: "1px solid var(--border-strong)",
            borderLeft: "2px solid " +
              (t.kind === "error" ? "var(--red)" : "var(--accent)"),
            borderRadius: "var(--r-9)", padding: "9px 14px",
            fontSize: "var(--fs-12)", color: "var(--text-2)",
            minWidth: 210, cursor: "pointer" }}>
          <b style={{ color: "var(--text)", fontWeight: 600 }}>{t.title}</b>
          {t.detail && <div style={{ marginTop: 3 }}>{String(t.detail)}</div>}
        </div>
      ))}
    </div>
  );
}
window.S2_Toasts = S2_Toasts;

// Menus mirror the palette: generated FROM the command registry by
// category, so a menu item and its shortcut are never defined twice
// (spec section 7: one verb table, three surfaces).
const S2_MENU_CATS = {
  File: ["create", "edit"],
  View: ["view"],
  Go: ["go"],
  Run: ["run", "workspace"],
};

function S2_Menus() {
  const [open, setOpen] = React.useState(null);
  React.useEffect(() => {
    if (open == null) return undefined;
    const close = () => setOpen(null);
    const onKey = (e) => { if (e.key === "Escape") close(); };
    window.addEventListener("click", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div style={{ display: "flex", gap: 2, position: "relative" }}
      data-testid="s2-menus">
      {Object.keys(S2_MENU_CATS).map((name) => (
        <div key={name} style={{ position: "relative" }}>
          <button
            aria-haspopup="true" aria-expanded={open === name}
            onClick={(e) => { e.stopPropagation(); setOpen(open === name ? null : name); }}
            style={{ padding: "4px 9px", borderRadius: "var(--r-6)",
              border: "none", cursor: "pointer", fontSize: "var(--fs-12)",
              background: open === name ? "var(--bg-hover)" : "none",
              color: open === name ? "var(--text)" : "var(--text-2)" }}>
            {name}
          </button>
          {open === name && (
            <div role="menu" style={{ position: "absolute", top: "100%",
              left: 0, minWidth: 230, zIndex: 90,
              background: "var(--bg-elev)",
              border: "1px solid var(--border-strong)",
              borderRadius: "var(--r-9)", padding: 5 }}>
              {window.S2_Commands.list()
                .filter((c) => S2_MENU_CATS[name].includes(c.cat))
                .map((c) => (
                  <button key={c.id} role="menuitem"
                    onClick={() => { setOpen(null); window.S2_Commands.run(c.id); }}
                    style={{ display: "flex", gap: 24, width: "100%",
                      alignItems: "center", justifyContent: "space-between",
                      textAlign: "left", padding: "6px 9px", cursor: "pointer",
                      borderRadius: "var(--r-6)", border: "none",
                      background: "none", color: "var(--text-2)",
                      fontSize: "var(--fs-12)" }}>
                    <span>{c.title}</span>
                    {c.shortcut && <span className="s2-kbd">{c.shortcut}</span>}
                  </button>
                ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
window.S2_Menus = S2_Menus;

function S2_Shell() {
  const [nav, setNav] = React.useState("work");
  const { useResource, apiFetch } = window.primerApi;
  const ctx = window.S2_Ctx.useCtx();
  const workspaces = useResource(
    "studio2:workspaces",
    (signal) => apiFetch("GET", "/workspaces?limit=200", null, { signal }),
    { pollMs: 30000 },
  );
  const wsRows = (workspaces.data && workspaces.data.items) || [];
  React.useEffect(() => { window.S2_registerCtxPins(wsRows); }, [wsRows.length]);
  const ctxName = ctx.ws
    ? ((wsRows.find((w) => w.id === ctx.ws) || {}).name || ctx.ws)
    : "none";
  const sessions = useResource(
    "studio2:sessions",
    (signal) => apiFetch("GET", "/sessions?limit=200", null, { signal }),
    { pollMs: 5000 },
  );
  const sessionRows = (sessions.data && sessions.data.items) || [];
  const nRunning = sessionRows.filter(
    (s) => String(s.status || "").toLowerCase() === "running").length;
  const nWaiting = sessionRows.filter(
    (s) => String(s.status || "").toLowerCase() === "waiting").length;
  React.useEffect(() => { window.S2_Docs.restore(); }, []);
  React.useEffect(() => {
    let chord = null;
    let chordTimer = null;
    // Exposed on window so legacy iframes (same-origin) can forward
    // their keydown events here - shell shortcuts must work no matter
    // which document has focus. e.target (not document.activeElement)
    // identifies inputs correctly across both documents.
    const onKey = (e) => {
      const mod = e.metaKey || e.ctrlKey;
      const t = e.target;
      const inInput = /^(INPUT|TEXTAREA|SELECT)$/.test((t && t.tagName) || "");
      if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); window.S2_openPalette("cmd"); return; }
      if (mod && e.key.toLowerCase() === "p") { e.preventDefault(); window.S2_openPalette("open"); return; }
      if (mod && e.key.toLowerCase() === "w") { e.preventDefault(); window.S2_Commands.run("tab:close"); return; }
      if (inInput) {
        // Esc always walks focus OUT of an input, so chords stay
        // reachable (design-pack keyboard discipline).
        if (e.key === "Escape" && t && t.blur) { e.preventDefault(); t.blur(); }
        return;
      }
      if (chord === "g") {
        clearTimeout(chordTimer);
        chord = null;
        window.S2_Commands.run("nav:" + e.key);
        return;
      }
      if (e.key === "g") {
        chord = "g";
        chordTimer = setTimeout(() => { chord = null; }, 1400);
      }
    };
    window.S2_handleKeydown = onKey;
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (window.S2_handleKeydown === onKey) window.S2_handleKeydown = null;
    };
  }, []);
  return (
    <div className="s2-root" data-testid="s2-root">
      <style>{S2_STYLE}</style>
      <div className="s2-menubar" data-testid="s2-menubar">
        <b style={{ fontWeight: 600 }}>primer</b>
        <span style={{ color: "var(--text-3)" }}>studio</span>
        <S2_Menus />
        <button data-testid="s2-ctx-chip"
          title="Workspace context scopes files and terminal. It follows the active session tab unless pinned via the palette."
          onClick={() => window.S2_openPalette("cmd")}
          style={{ display: "flex", gap: 6, alignItems: "center",
            marginLeft: 14, padding: "4px 10px", cursor: "pointer",
            borderRadius: "var(--r-6)", background: "var(--bg-2)",
            border: "1px solid var(--border)", color: "var(--text)",
            fontSize: "var(--fs-12)" }}>
          <span style={{ color: "var(--text-3)" }}>ctx:</span>
          <b style={{ fontWeight: 600 }}>{ctxName}</b>
          <span style={{ color: "var(--text-3)" }}>
            {ctx.pinned ? "· pinned" : "· auto"}
          </span>
        </button>
        <span style={{ flex: 1 }} />
        <a href="#/" style={{ color: "var(--text-3)", fontSize: "var(--fs-12)" }}>
          exit trial
        </a>
      </div>
      <div className="s2-mid">
        <div className="s2-rail" data-testid="s2-rail">
          <window.S2_Rail nav={nav} setNav={setNav} />
        </div>
        <div className="s2-nav" data-testid="s2-nav">
          <window.S2_Nav nav={nav} />
        </div>
        <div className="s2-center" data-testid="s2-center">
          <window.S2_TabBar />
          <window.S2_ActiveDoc />
          <div className="s2-term" data-testid="s2-term" style={{ height: 0 }} />
        </div>
        <div className="s2-right" data-testid="s2-right">
          <window.S2_Right />
        </div>
      </div>
      <div className="s2-status" data-testid="s2-status">
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ width: 7, height: 7, borderRadius: "50%",
            background: "var(--green)" }} />
          {nRunning} running
        </span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ width: 7, height: 7, borderRadius: "50%",
            background: "var(--amber)" }} />
          {nWaiting} waiting
        </span>
        <span>ctx {ctxName}{ctx.pinned ? " (pinned)" : ""}</span>
        <span style={{ flex: 1 }} />
        <span>g+letter navigators · {(window.S2_MOD || "Ctrl")}K palette</span>
        <span>{window.S2_TRIAL_BADGE_TEXT}</span>
      </div>
      <div className="s2-gate">
        <div>
          <p>The Studio trial is desktop-only for now.</p>
          <p><a href="#/">Back to the classic console</a></p>
        </div>
      </div>
      <window.S2_Palette />
      <S2_Toasts />
    </div>
  );
}
window.S2_Shell = S2_Shell;
