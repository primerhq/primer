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
