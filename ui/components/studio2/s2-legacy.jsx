/* global React */
// Legacy documents: un-migrated console features render as same-origin
// iframes of the classic console. This makes the trial COMPLETE on day
// one; each feature's native migration later deletes its row here.
// Requires frame-ancestors 'self' on /console (see _app_middleware.py).
window.S2_LEGACY_ROUTES = [
  { ref: "/",                                title: "Dashboard",            group: "system" },
  { ref: "/agents",                          title: "Agents",               group: "agents" },
  { ref: "/graphs",                          title: "Graphs",               group: "graphs" },
  { ref: "/workspaces",                      title: "Workspaces",           group: "work" },
  { ref: "/workspaces/templates",            title: "Templates",            group: "work" },
  { ref: "/workspaces/providers",            title: "WS Providers",         group: "work" },
  { ref: "/knowledge/collections",           title: "Collections",          group: "knowledge" },
  { ref: "/knowledge/documents",             title: "Documents",            group: "knowledge" },
  { ref: "/subsystems/internal-collections", title: "Internal Collections", group: "knowledge" },
  { ref: "/ssp",                             title: "Semantic Search",      group: "knowledge" },
  { ref: "/providers",                       title: "Providers",            group: "compute" },
  { ref: "/toolsets",                        title: "Toolsets",             group: "toolsets" },
  { ref: "/tools",                           title: "Tools",                group: "toolsets" },
  { ref: "/channels/channels",               title: "Channels",             group: "autom" },
  { ref: "/channels/rules",                  title: "Channel Rules",        group: "autom" },
  { ref: "/triggers",                        title: "Triggers",             group: "autom" },
  { ref: "/services",                        title: "Services",             group: "services" },
  { ref: "/harnesses",                       title: "Harnesses",            group: "system" },
  { ref: "/workers",                         title: "Workers",              group: "system" },
  { ref: "/health",                          title: "Health",               group: "system" },
  { ref: "/admin/users",                     title: "Users",                group: "system" },
  { ref: "/admin/sso-providers",             title: "SSO Providers",        group: "system" },
  { ref: "/settings/api-tokens",             title: "API Tokens",           group: "system" },
  { ref: "/settings/linked-accounts",        title: "Linked Accounts",      group: "system" },
  { ref: "/settings/mcp",                    title: "MCP Server",           group: "system" },
  { ref: "/docs",                            title: "Docs",                 group: "system" },
];

function S2_legacyFrame(hashPath) {
  return (
    <iframe
      title={"classic console " + hashPath}
      data-testid="s2-legacy-frame"
      src={"./#" + hashPath}
      onLoad={(e) => {
        // Same-origin: forward the inner document's keydown to the
        // shell so palette/chords/tab shortcuts work while a legacy
        // frame has focus.
        try {
          e.target.contentWindow.addEventListener("keydown", (ev) => {
            if (window.S2_handleKeydown) window.S2_handleKeydown(ev);
          });
        } catch (_err) { /* cross-origin frame: nothing to forward */ }
      }}
      style={{ width: "100%", height: "100%", border: "none",
        display: "block", background: "var(--bg)", flex: 1 }}
    />
  );
}

window.S2_Docs.registerKind("legacy", {
  glyph: "▤",
  title: (ref) => {
    const row = window.S2_LEGACY_ROUTES.find((r) => r.ref === ref);
    return (row ? row.title : ref) + " (classic)";
  },
  render: (ref) => S2_legacyFrame(ref),
});

// Interim detail kinds: session/agent documents render the classic
// detail pages until the NATIVE documents (plan tasks 8 and 10)
// re-register these kinds. registerKind overwrites, and those files
// load after this one, so the native versions win once they exist.
window.S2_Docs.registerKind("session", {
  glyph: "▣",
  title: (ref) => ref,
  render: (ref) => S2_legacyFrame("/sessions/" + ref),
});
window.S2_Docs.registerKind("agent", {
  glyph: "◆",
  title: (ref) => ref,
  render: (ref) => S2_legacyFrame("/agents/" + ref),
});
