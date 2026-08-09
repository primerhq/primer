/* global React */
// Palette overlay: mode "cmd" runs commands, mode "open" opens nouns from
// S2_QuickIndex (registered by the navigator task; empty until then).
// Scrim at 45% black per the design-pack modal rule; everything else
// rides console tokens.
function S2_fuzzy(q, s) {
  q = q.toLowerCase();
  s = s.toLowerCase();
  if (!q) return 1;
  if (s.includes(q)) return s.startsWith(q) ? 5 : 3;
  let i = 0;
  for (const ch of s) if (ch === q[i]) i += 1;
  return i === q.length ? 1 : 0;
}
window.S2_fuzzy = S2_fuzzy;

function S2_Palette() {
  const [mode, setMode] = React.useState(null);
  const [q, setQ] = React.useState("");
  const [sel, setSel] = React.useState(0);
  const inputRef = React.useRef(null);

  React.useEffect(() => {
    window.S2_openPalette = (m) => {
      setMode(m);
      setQ("");
      setSel(0);
    };
    return () => {
      window.S2_openPalette = () => {};
    };
  }, []);
  React.useEffect(() => {
    if (mode && inputRef.current) inputRef.current.focus();
  }, [mode]);

  if (!mode) return null;
  const source =
    mode === "cmd"
      ? window.S2_Commands.list().map((c) => ({
          key: c.id,
          glyph: c.glyph || "⌘",
          label: c.title,
          cat: c.cat || "",
          kbd: c.shortcut,
          go: () => window.S2_Commands.run(c.id),
        }))
      : ((window.S2_QuickIndex && window.S2_QuickIndex.items()) || []);
  const items = source
    .map((it) => ({ ...it, score: S2_fuzzy(q, it.label) }))
    .filter((it) => it.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 14);
  const close = () => setMode(null);
  const exec = (it) => {
    close();
    it.go();
  };
  return (
    <div
      data-testid="s2-palette"
      style={{ position: "fixed", inset: 0, zIndex: 80,
        background: "rgba(0,0,0,.45)", display: "flex",
        justifyContent: "center", alignItems: "flex-start",
        paddingTop: "11vh" }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}
    >
      <div style={{ width: "min(560px,92vw)", background: "var(--bg-elev)",
        border: "1px solid var(--border-strong)",
        borderRadius: "var(--r-12)", overflow: "hidden" }}>
        <input
          ref={inputRef}
          data-testid="s2-palette-input"
          aria-label={mode === "cmd" ? "Run a command" : "Open anything"}
          placeholder={mode === "cmd" ? "Run a command…" : "Open anything…"}
          value={q}
          onChange={(e) => { setQ(e.target.value); setSel(0); }}
          onKeyDown={(e) => {
            if (e.key === "Escape") close();
            else if (e.key === "ArrowDown") { e.preventDefault(); setSel(Math.min(items.length - 1, sel + 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setSel(Math.max(0, sel - 1)); }
            else if (e.key === "Enter" && items[sel]) { e.preventDefault(); exec(items[sel]); }
          }}
          style={{ width: "100%", background: "none", border: "none",
            borderBottom: "1px solid var(--border)", padding: "13px 16px",
            color: "var(--text)", fontSize: 14, outline: "none" }}
        />
        <div role="listbox" style={{ maxHeight: "46vh", overflowY: "auto", padding: 6 }}>
          {items.map((it, i) => (
            <button
              key={it.key}
              role="option"
              aria-selected={i === sel}
              onClick={() => exec(it)}
              style={{ display: "flex", gap: 10, width: "100%",
                alignItems: "center", textAlign: "left", padding: "7px 11px",
                cursor: "pointer", borderRadius: "var(--r-6)", border: "none",
                background: i === sel ? "var(--bg-active)" : "none",
                color: "var(--text-2)", fontSize: "var(--fs-12)" }}
            >
              <span style={{ width: 15, textAlign: "center",
                color: "var(--text-3)", fontSize: 11 }}>{it.glyph}</span>
              <span style={{ flex: 1, overflow: "hidden",
                textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{it.label}</span>
              <span style={{ fontSize: 10, color: "var(--text-4)",
                textTransform: "uppercase", letterSpacing: ".06em" }}>{it.cat}</span>
              {it.kbd && <span className="s2-kbd">{it.kbd}</span>}
            </button>
          ))}
          {!items.length && (
            <div style={{ padding: 16, color: "var(--text-4)",
              fontSize: "var(--fs-12)" }}>no matches</div>
          )}
        </div>
        <div style={{ borderTop: "1px solid var(--border)", padding: "6px 14px",
          fontSize: 10, color: "var(--text-4)", display: "flex", gap: 14 }}>
          <span>↑↓ navigate</span>
          <span>⏎ run</span>
          <span>esc close</span>
        </div>
      </div>
    </div>
  );
}
window.S2_Palette = S2_Palette;
