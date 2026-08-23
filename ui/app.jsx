/* global ReactDOM */

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
function AppRoot() {
  return (
    <window.AuthGate>
      <window.NV_Shell />
    </window.AuthGate>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<AppRoot />);
