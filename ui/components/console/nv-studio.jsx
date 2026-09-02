/* global React, NV_useConsole */
// The Studio frame (uiv2 R2 cutover, US-011a): left rail (Inbox +
// workspace tree, NV_Rail), center tab groups, always-visible right
// Files sidebar (notes 2.5), terminal panel and events sidebar slots.
// The old Sessions|Files rail-toggle and its flag-gated fallback to
// nv-sessions-sidebar.jsx/nv-doc-host.jsx retired with this round -
// NV_TG_ENABLED is gone, this is just how the shell works now.
// Also home of the pure helper tests share: the agent identity glyphs.

// The five seeded agents carry the prototype's glyph set; anything
// else gets a stable hash pick from the same vocabulary. Never
// human-passing (designer identity rule).
var NV_GLYPHS = {
  operator: { d: "M6 1 11 6 6 11 1 6Z", color: "var(--blue)" },
  builder: { d: "M6 1 10.33 3.5v5L6 11 1.67 8.5v-5Z", color: "var(--violet)" },
  planner: { d: "M6 1.6 11 10.4H1Z", color: "var(--teal)" },
  explorer: {
    d: "M6 0.8 7.4 4.6 11.2 6 7.4 7.4 6 11.2 4.6 7.4 0.8 6 4.6 4.6Z",
    color: "var(--amber)",
  },
  "tool-runner": { d: "M2.2 2.2h7.6v7.6H2.2Z", color: "var(--pink)" },
};
var NV_GLYPH_POOL = ["operator", "builder", "planner", "explorer", "tool-runner"];
var NV_GRAPH_GLYPH = {
  d: "M1 1.5h4.4v3.6H1Z M6.6 6.9H11v3.6H6.6Z",
  color: "var(--text-3)",
};

function NV_identity(binding) {
  if (!binding) return NV_GLYPHS.operator;
  if (binding.kind === "graph") return NV_GRAPH_GLYPH;
  var id = binding.agent_id || "agent";
  if (NV_GLYPHS[id]) return NV_GLYPHS[id];
  var sum = 0;
  for (var i = 0; i < id.length; i++) sum = (sum * 31 + id.charCodeAt(i)) % 997;
  return NV_GLYPHS[NV_GLYPH_POOL[sum % NV_GLYPH_POOL.length]];
}

// Lifted near-verbatim from nv-doc-host.jsx (US-007 R2 phase 2): the kind
// dispatch and empty-state markup survive unchanged, now called by
// NV_TabGroups once per group (once for the sole group phase 2 ships
// with) instead of once for the whole center. `con` closes over the verb
// registry for the empty state's New session / Ctrl+K buttons.
function NV_renderStudioDoc(con, tab) {
  if (!tab) {
    return (
      <div className="nv-center-empty" data-testid="nv-center-empty">
        <svg width="34" height="34" viewBox="0 0 24 24"
          style={{ marginBottom: 10, color: "var(--text-4)" }}>
          <polygon points="12,3 21,12 12,21 3,12" fill="none"
            stroke="currentColor" strokeWidth="1.2" />
          <polygon points="12,12 16.5,16.5 12,21 7.5,16.5"
            fill="var(--brand-green)" />
        </svg>
        <div>Nothing open. Pick a session, or start one.</div>
        <div className="nv-center-empty-actions">
          <button type="button" className="nv-btn-primary"
            data-testid="nv-empty-new-session"
            onClick={function () {
              var verb = con.registry.get("session.create");
              if (verb) verb.run();
            }}>New session</button>
          <button type="button" className="nv-btn-secondary"
            onClick={function () {
              var verb = con.registry.get("palette.open");
              if (verb) verb.run();
            }}>Ctrl+K commands</button>
        </div>
      </div>
    );
  }
  // SEV fix: this dispatcher renders from the same fixed tree position
  // regardless of which tab is active (renderDoc(activeTab) is called
  // from one un-keyed slot in nv-tab-groups.jsx), so switching tabs
  // without a `key` re-renders the SAME doc component instance with
  // new props instead of unmount+remount. For NV_SessionDoc that meant
  // a brand-new session's tab could render with a stale in-flight
  // useResource snapshot from the PREVIOUS session still in scope for
  // one render, long enough for the REST-history seed effect to apply
  // the old session's messages into the new session's store (which
  // carries no session_id to filter on - see session-store.js's SS_apply
  // fix). Keying by the tab's own ref forces a real remount on switch,
  // closing the window entirely rather than relying on a downstream
  // guard.
  if (tab.kind === "session" && typeof window.NV_SessionDoc === "function") {
    return <window.NV_SessionDoc key={tab.ref} sid={tab.ref} />;
  }
  if (tab.kind === "file" && typeof window.NV_FileDoc === "function") {
    return <window.NV_FileDoc key={tab.ref} path={tab.ref} />;
  }
  if (tab.kind === "diff" && typeof window.NV_DiffDoc === "function") {
    return <window.NV_DiffDoc key={tab.ref} sha={tab.ref} />;
  }
  if (tab.kind === "wiki" && typeof window.NV_WikiDoc === "function") {
    return <window.NV_WikiDoc key={tab.ref} slug={tab.ref} />;
  }
  return null;
}

function NV_Studio() {
  var con = NV_useConsole();

  function renderDoc(tab) { return NV_renderStudioDoc(con, tab); }

  return (
    <div className="nv-studio" data-testid="nv-studio">
      <div className="nv-rail" data-testid="nv-rail">
        <window.NV_Rail
          selectedWorkspaceId={con.wid}
          onSelectWorkspace={function (wid) {
            var verb = con.registry.get("workspace.switch");
            if (verb) verb.run({ wid: wid });
          }}
          onOpenSession={function (session, wid) {
            // F2 (2026-08-29 UI review): route the cross-workspace case
            // through the single combined navigation (con.openInWorkspace)
            // instead of a separate workspace.switch + setDoc, each with
            // their own markPush - that used to push two history entries
            // for one navigation.
            if (wid && wid !== con.wid && con.openInWorkspace) {
              con.openInWorkspace(wid, { kind: "session", ref: session.session_id });
            } else {
              con.setDoc({ kind: "session", ref: session.session_id });
            }
            if (con.promoteDoc) {
              con.promoteDoc("session:" + session.session_id);
            }
            // F1/F10 (2026-08-29 UI review): the tab's label, glyph and
            // pulse all resolve from a 5s poll; the rail's own row already
            // carries the full session (name, binding), so stamp it
            // immediately rather than showing the raw id/default glyph
            // until the poll catches up.
            if (con.stampSessionMeta) {
              con.stampSessionMeta(session.session_id, {
                wid: wid, name: session.name, binding: session.binding,
              });
            }
          }}
          onCreateSessionInWorkspace={function (wid) {
            if (con.createSessionInWorkspace) con.createSessionInWorkspace(wid);
          }}
          onCreateWorkspace={function () {
            var verb = con.registry.get("workspace.create");
            if (verb) verb.run();
          }}
          onCreateSession={function () {
            var verb = con.registry.get("session.create");
            if (verb) verb.run();
          }}
          onOpenWorkspaceSettings={function (wid) {
            con.openOverlay("workspaces", "detail", wid);
          }}
        />
      </div>
      <div className="nv-center" data-testid="nv-center">
        <window.NV_TabGroups
          model={con.tgModel}
          onModelChange={con.onTgModelChange}
          renderDoc={renderDoc}
          resolveSessionMeta={con.resolveSessionMeta}
        />
        {con.panels.terminal && typeof window.NV_Terminal === "function"
          ? <window.NV_Terminal />
          : null}
      </div>
      {/* Always-visible only WITH a workspace: the panel's tree/log polls
          key off con.wid, and mounting it wid-less fires literal
          /workspaces/null/... fetches (404 noise on every bare route). */}
      {con.wid && typeof window.NV_FilesSidebar === "function"
        ? <window.NV_FilesSidebar />
        : null}
    </div>
  );
}

// Usage summary for the session header's mini meter. The session row
// carries usage when the backend provides it; absent data renders an
// empty meter rather than a lie.
function NV_usageOf(session) {
  var u = session && session.usage;
  if (!u || !u.total_input_tokens) return { pct: 0, label: "" };
  var ctx = session.context_length || 0;
  var used = (u.total_input_tokens || 0) + (u.total_output_tokens || 0);
  var pct = ctx ? Math.min(100, Math.round((used / ctx) * 100)) : 0;
  var usedLabel = Math.round(used / 1000) + "k";
  // UX reconcile wave 1 (audit A item 13): used ALONE ("38k") answered
  // "how much so far" but not "out of what" - the budget is exactly what
  // ctx already carries, so this was a label bug, not missing data. No
  // ctx (rare) keeps the old used-only label rather than showing a
  // misleading "/ 0k".
  var label = ctx ? usedLabel + " / " + Math.round(ctx / 1000) + "k" : usedLabel;
  return { pct: pct, label: label };
}

window.NV_usageOf = NV_usageOf;
window.NV_GLYPHS = NV_GLYPHS;
window.NV_identity = NV_identity;
window.NV_Studio = NV_Studio;
