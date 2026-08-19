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
  agents: {
    render: function () {
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
    render: function () {
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
    render: function () {
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
    render: function () {
      return (
        <window.WorkspacesPage
          pushToast={window.primerApi.toastPush}
          onOpen={function (wid) { window.SH_OVERLAY_SWITCH_WORKSPACE(wid); }}
        />
      );
    },
  },
};

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
        <button type="button" className="sh-verb"
          data-testid="shell-overlay-close"
          onClick={function () { shell.closeOverlay(); }}>Close Overlay</button>
      </div>
      <div className="sh-overlay-body" data-testid="shell-overlay-body">
        {mount.render(overlay)}
      </div>
    </div>
  );
}

window.SH_OVERLAY_MOUNTS = SH_OVERLAY_MOUNTS;
window.SH_OverlayHost = SH_OverlayHost;
