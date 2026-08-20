/* global React, SH_useShell, SH_OVERLAYS */
// Overlays: fourteen self-contained management surfaces, each addressable
// as overlay=<name>[:<section>[:<id>]] and each a SHALLOW one-decision
// surface. Deep editing and comparison are tab-docs, so no doc kind ever
// appears in this table.
//
// The no-chrome contract: a mount renders the page component and nothing
// else. No sidebar, no topbar, no nav. Sub-state arrives through the
// overlay segments, translated by foundation/shell-router-shim.js.
//
// Task 18 lands the catalog (prebuilt, spec section 7). Task 19 adds the
// other twelve reused pages; Task 20 adds the designer-fresh admin.

var SH_OVERLAY_MOUNTS = {
  admin: {
    // Sections carry their own role gates, so the overlay itself opens
    // for anyone: a non-admin sees the sections they are allowed.
    render: function (state) {
      return <window.SH_AdminOverlay section={state.section} id={state.id} />;
    },
  },
  providers: {
    // S4's standalone-mountable catalog (ui/components/provider-catalog.jsx,
    // the M11d props-only contract). The class comes off the section segment
    // and the instance off the id segment, so overlay=providers:tts:<id>
    // addresses one provider. The per-class provider pages died in S4 P4;
    // the catalog is the only provider surface, and naming a dead global
    // here is what this file's guard exists to prevent.
    render: function (state) {
      return (
        <window.ProviderCatalog
          initialClass={state.section || "llm"}
          initialInstanceId={state.id || null}
          onNavigate={function (name, section, id) {
            window.SH_openOverlayFromShim(name, section, id);
          }}
        />
      );
    },
  },
  collections: {
    render: function (state) {
      return (
        <window.CollectionsPage
          pushToast={window.primerApi.toastPush}
          onOpen={function (cid) {
            window.SH_OVERLAY_OPEN_DOC("wiki", cid + "/" + (state.id || ""));
          }}
          onNavigate={function () {}}
        />
      );
    },
  },
  // List and record are ONE overlay: the id slot decides which renders,
  // so clicking a row and pasting a link land in the same place.
  agents: {
    render: function (state) {
      if (state.id) {
        return (
          <window.AgentDetail
            agentId={state.id}
            pushToast={window.primerApi.toastPush}
          />
        );
      }
      return (
        <window.AgentsPage
          pushToast={window.primerApi.toastPush}
          onOpen={function (aid) {
            window.SH_OVERLAY_OPEN_OVERLAY("agents", null, aid);
          }}
        />
      );
    },
  },
  graphs: {
    render: function (state) {
      if (state.id) {
        return (
          <window.GraphDetail
            graphId={state.id}
            pushToast={window.primerApi.toastPush}
          />
        );
      }
      return (
        <window.GraphsPage
          pushToast={window.primerApi.toastPush}
          onOpen={function (gid) {
            window.SH_OVERLAY_OPEN_OVERLAY("graphs", null, gid);
          }}
        />
      );
    },
  },
  triggers: {
    render: function (state) {
      return <window.TR_TriggersPage triggerId={state.id || null} />;
    },
  },
  toolsets: {
    render: function (state) {
      if (state.id) {
        return (
          <window.ToolsetDetail
            toolsetId={state.id}
            pushToast={window.primerApi.toastPush}
          />
        );
      }
      return <window.ToolsetsPage pushToast={window.primerApi.toastPush} />;
    },
  },
  tools: {
    render: function () {
      return <window.ToolsPage pushToast={window.primerApi.toastPush} />;
    },
  },
  // Workers + Health collapse into one overlay (pinned decision 3):
  // health.jsx exposes a single small panel, so it is a section here
  // rather than a fifteenth registry key.
  workers: {
    render: function (state) {
      if (state.section === "health") return <window.HealthPage sessions={null} />;
      return <window.WorkersPage pushToast={window.primerApi.toastPush} />;
    },
  },
  approvals: {
    render: function () {
      return (
        <window.ApprovalsPage
          pushToast={window.primerApi.toastPush}
          onNavigate={function (_page, sid) {
            if (sid) window.SH_OVERLAY_OPEN_DOC("session", sid);
          }}
        />
      );
    },
  },
  // Lazy creation covers the FIRST session in an empty workspace; this is
  // how an operator starts any session after that, with a binding they
  // pick rather than the system default.
  "new-session": {
    render: function (state, shell) {
      return (
        <window.SharedNewSessionForm
          wid={shell.wid}
          onCreated={function (row) {
            var sid = row && (row.session_id || row.id);
            shell.closeOverlay();
            if (sid) shell.openDoc({ kind: "session", ref: sid, preview: false });
          }}
          onCancel={function () { shell.closeOverlay(); }}
          pushToast={window.primerApi && window.primerApi.toastPush}
        />
      );
    },
  },
  harnesses: {
    render: function (state) {
      return <window.HarnessesPage harnessId={state.id || null} />;
    },
  },
  services: {
    render: function (state) {
      return <window.SV_ServicesPage serviceId={state.id || null} />;
    },
  },
  // Instances plus rules, the pairing section 3 asks for.
  channels: {
    render: function (state) {
      if (state.section === "rules") {
        return <window.ChannelRulesPage pushToast={window.primerApi.toastPush} />;
      }
      return (
        <window.ChannelsPage
          pushToast={window.primerApi.toastPush}
          onNavigate={function () {}}
        />
      );
    },
  },
  workspaces: {
    render: function (state, shell) {
      // ?overlay=workspaces:detail:<wid> opens ONE workspace's own tabs
      // (channels, config, log, destroy). Without it the overlay can only
      // switch which workspace the shell is in, and a workspace's own
      // settings have nowhere to live.
      // Templates are workspace-shaped configuration, so they belong to
      // this overlay rather than a surface of their own.
      if (state.section === "templates") {
        return (
          <window.WorkspaceTemplatesPage
            pushToast={window.primerApi.toastPush}
          />
        );
      }
      if (state.section === "detail" && state.id) {
        return (
          <window.WorkspaceDetail
            workspaceId={state.id}
            pushToast={window.primerApi.toastPush}
            onNavigate={function () {}}
            onOpenSession={function (sid) {
              shell.closeOverlay();
              shell.openDoc({ kind: "session", ref: sid, preview: false });
            }}
          />
        );
      }
      return (
        <window.WorkspacesPage
          pushToast={window.primerApi.toastPush}
          onOpen={function (wid) { window.SH_OVERLAY_SWITCH_WORKSPACE(wid); }}
        />
      );
    },
  },
};

// The heading each overlay shows above its page. Distinct from
// SH_OVERLAY_LABELS, which is palette wording ("Open Agents") for the
// verb that gets you here and names no record.
var SH_OVERLAY_TITLES = {
  "new-session": "New session",
  providers: "Providers",
  collections: "Collections",
  agents: "Agents",
  graphs: "Graphs",
  triggers: "Triggers",
  toolsets: "Toolsets",
  tools: "Tools",
  workers: "Workers",
  approvals: "Approvals",
  admin: "Admin settings",
  harnesses: "Harnesses",
  services: "Services",
  channels: "Channels",
  workspaces: "Workspaces",
};


// Some surfaces are addressed by SECTION rather than by record, and the
// section is what the page is actually showing: "channels:rules" is the
// channel rules, not channels. Titling those by the surface alone names
// the wrong page.
var SH_OVERLAY_SECTION_TITLES = {
  "channels:rules": "Channel rules",
  "workspaces:templates": "Workspace templates",
  "workers:health": "Health",
};


function SH_overlayTitle(overlay) {
  // A detail view is titled by the record it is showing; a list view by
  // the surface. app.jsx rendered this header for every route before S8
  // re-hosted the pages in overlays, and the pages themselves never had
  // one, so without it nothing on screen says which agent (or graph, or
  // toolset) you are looking at.
  if (overlay.id) return overlay.id;
  if (overlay.section) {
    var keyed = SH_OVERLAY_SECTION_TITLES[
      overlay.name + ":" + overlay.section
    ];
    if (keyed) return keyed;
  }
  return SH_OVERLAY_TITLES[overlay.name] || overlay.name;
}


function SH_OverlayHost() {
  var shell = SH_useShell();
  var overlay = shell.overlay;

  // The shim reads live overlay state, so it is installed once and then
  // simply answers whatever the current overlay is.
  var installedRef = React.useRef(false);
  var overlayRef = React.useRef(overlay);
  overlayRef.current = overlay;
  if (!installedRef.current) {
    installedRef.current = true;
    window.SH_installRouterShim(
      function () { return overlayRef.current; },
      function (name, section, id) { shell.openOverlay(name, section, id); }
    );
  }

  if (!overlay || !overlay.name) return null;
  var name = overlay.name;
  var mount = SH_OVERLAY_MOUNTS[name];
  if (!mount) return null;
  if (mount.roles && mount.roles.indexOf(shell.role) < 0) {
    return (
      <div className="sh-overlay" data-testid={"shell-overlay:" + name}>
        <div className="sh-overlay-denied">
          This surface needs a different role.
        </div>
      </div>
    );
  }

  return (
    <div className="sh-overlay" role="dialog" aria-modal="false"
      data-testid={"shell-overlay:" + name}>
      <div className="sh-overlay-bar">
        <span className="sh-overlay-title">
          {window.SH_OVERLAY_LABELS[name]}
        </span>
        {/* Rendered from the registry so an overlay-surface verb cannot
            be declared and then never offered anywhere. */}
        <span className="sh-overlay-verbs">
          {shell.registry.forSurface("overlay-button").map(function (verb) {
            return (
              <button type="button" key={verb.id} className="sh-verb"
                data-verb={verb.id}
                onClick={function () { verb.run(); }}>{verb.label}</button>
            );
          })}
        </span>
        <button type="button" className="sh-verb"
          data-testid="shell-overlay-close"
          onClick={function () { shell.closeOverlay(); }}>Close Overlay</button>
      </div>
      <div className="sh-overlay-body" data-testid="shell-overlay-body">
        <div className="page-header">
          <h1 className="page-title">{SH_overlayTitle(overlay)}</h1>
        </div>
        {mount.render(overlay, shell)}
      </div>
    </div>
  );
}

window.SH_OVERLAY_MOUNTS = SH_OVERLAY_MOUNTS;
window.SH_OVERLAY_TITLES = SH_OVERLAY_TITLES;
window.SH_OVERLAY_SECTION_TITLES = SH_OVERLAY_SECTION_TITLES;
window.SH_overlayTitle = SH_overlayTitle;
window.SH_OverlayHost = SH_OverlayHost;
