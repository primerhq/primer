/* global React, ReactDOM, Btn */

// The console IS the three-view shell (2026-08-23 designer handoff,
// flag day at wiring plan P7). One surface, one gate: AuthGate owns
// the whole boot branch (register/login -> forced password change ->
// restricted -> setup wizard for admins, waiting screen for everyone
// else) and returns children once the install is complete.
//
// Everything this file used to hold is gone with the flag days: the
// page dispatch, the route table, the navigate helper, the topbar
// mount, every page header, and lastly the preview tweak that gated
// NV_Shell while it was wired. Studio / Platform / System are views
// in the URL hash, a management surface is an overlay, and a session
// is a document.

// The console runs a PRODUCTION React build with no dev overlay, so a
// render crash on any surface outside the graph builder used to
// unmount the whole tree: a blank page, the URL-held selection gone,
// and any in-memory draft lost. This boundary is the BACKSTOP above
// AuthGate / NV_Shell; the graph builder's own scoped GB_ErrorBoundary
// (ui/components/graph-builder/graph-builder.jsx) still recovers in
// place first for builder crashes. A full reload is the honest
// recovery at the root: there is no draft above the crash and no
// standing surface to retry on.
class RootErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error: error || new Error("the console failed to draw") };
  }

  componentDidCatch(error) {
    // React caught this render error here, so no window error event
    // fires for it (the global handlers below never see it, no
    // double-report); the console line is the operator's other record.
    console.error("primer console root crash:", error);
  }

  render() {
    if (!this.state.error) return this.props.children;
    const error = this.state.error;
    const detail = error && error.message ? error.message : String(error);
    return (
      <div className="col" data-testid="nv-root-crash" role="alert" style={{ gap: 10, padding: 20, border: "1px solid var(--border)", borderRadius: 12, background: "var(--bg-1)" }}>
        <div style={{ fontWeight: 600 }}>A screen in the console crashed.</div>
        <div className="muted" style={{ fontSize: "var(--fs-12)" }}>
          The console cannot keep drawing. Reloading restarts it: the selection in the URL comes back, but anything that was only in memory is gone.
        </div>
        <div className="mono" data-testid="nv-root-crash-detail" style={{ fontSize: "var(--fs-11)", color: "var(--text-3)", wordBreak: "break-word" }}>{detail}</div>
        <div className="row" style={{ gap: 8 }}>
          <Btn size="sm" data-testid="nv-root-crash-reload" onClick={function () { window.location.reload(); }}>Reload the console</Btn>
        </div>
      </div>
    );
  }
}

function AppRoot() {
  return (
    <window.AuthGate>
      <window.NV_Shell />
    </window.AuthGate>
  );
}

// An unhandled promise rejection (or an uncaught throw outside React) in
// the console document used to be invisible: a JS-console warning only.
// These route the reason to the console's one toast stack -
// primerApi.toastPush, which NV_ToastHost takes over while it is mounted
// (foundation/toast.js owns the queue until then). Render crashes do NOT
// reach these handlers: React catches them at RootErrorBoundary, so the
// window handlers see only uncaught script errors and unhandled
// rejections - disjoint paths, no double toast.
function NV_rootReport(message) {
  const api = window.primerApi;
  if (api && typeof api.toastPush === "function") {
    api.toastPush({ kind: "error", text: String(message) });
  }
}

window.addEventListener("error", function (ev) {
  // Resource-load failures (img / script tags) dispatch here with the
  // failing element as the target; uncaught script errors dispatch on
  // window. Only the latter belong in a toast.
  if (ev && ev.target && ev.target !== window) return;
  NV_rootReport(ev && typeof ev.message === "string" ? ev.message : "an uncaught error");
});
window.addEventListener("unhandledrejection", function (ev) {
  const reason = ev && ev.reason;
  NV_rootReport(
    reason && typeof reason.message === "string"
      ? reason.message
      : reason != null
        ? String(reason)
        : "a promise was rejected"
  );
});

ReactDOM.createRoot(document.getElementById("root")).render(
  <RootErrorBoundary>
    <AppRoot />
  </RootErrorBoundary>
);
