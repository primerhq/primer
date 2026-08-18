/* global React */
// The activity rail: one icon per feature group, sized to the audited
// console IA (spec section 4). Chords g+<letter> registered here.
window.S2_NAV_GROUPS = [
  { id: "work",      glyph: "▣", label: "Work",        chord: "s" },
  { id: "agents",    glyph: "◆", label: "Agents",      chord: "a" },
  { id: "graphs",    glyph: "◈", label: "Graphs",      chord: "g" },
  { id: "knowledge", glyph: "▤", label: "Knowledge",   chord: "k" },
  { id: "files",     glyph: "▢", label: "Files",       chord: "f" },
  { id: "compute",   glyph: "⚙", label: "Compute",     chord: "m" },
  { id: "toolsets",  glyph: "⚒", label: "Toolsets",    chord: "t" },
  { id: "autom",     glyph: "⏱", label: "Automations", chord: "u" },
  { id: "services",  glyph: "☁", label: "Services",    chord: "v" },
  { id: "system",    glyph: "⚕", label: "System",      chord: "y" },
];

function S2_Rail({ nav, setNav }) {
  React.useEffect(() => {
    for (const g of window.S2_NAV_GROUPS) {
      window.S2_Commands.register({
        id: "nav:" + g.chord, title: g.label + " navigator",
        glyph: g.glyph, cat: "go", shortcut: "g " + g.chord,
        run: () => setNav(g.id),
      });
    }
  }, [setNav]);
  return (
    <div role="tablist" aria-label="Features" data-testid="s2-rail-list"
      style={{ display: "flex", flexDirection: "column", alignItems: "center",
        gap: 2, padding: "var(--sp-2) 0" }}>
      {window.S2_NAV_GROUPS.map((g) => (
        <button key={g.id} role="tab" aria-selected={nav === g.id}
          aria-label={g.label} title={g.label + "  (g " + g.chord + ")"}
          onClick={() => setNav(g.id)}
          style={{ width: 34, height: 34, borderRadius: "var(--r-6)",
            border: "none", cursor: "pointer", fontSize: 15,
            background: nav === g.id ? "var(--accent-dim)" : "none",
            color: nav === g.id ? "var(--accent)" : "var(--text-3)" }}>
          {g.glyph}
        </button>
      ))}
    </div>
  );
}
window.S2_Rail = S2_Rail;
