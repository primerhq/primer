/* global ReactDOM */

// The console IS the studio (S8). One surface, one gate: AuthGate owns
// the whole boot branch (register/login -> forced password change ->
// restricted -> setup wizard for admins, waiting screen for everyone
// else) and returns children once the install is complete.
//
// Everything this file used to hold is gone with the flag day: the page
// dispatch, the route table, the navigate helper, the topbar mount and
// every page header. A management surface is an overlay on the shell,
// addressed in the URL hash, and a session is a document.
// consoleNext (persisted tweak): mounts the three-view console being
// wired from the 2026-08-23 designer handoff instead of the current
// shell. Same gate chain either way; flag dies at the wiring plan's
// flag day (P7).
function AppRoot() {
  const [tweaks] = window.useTweaks();
  const next = tweaks.consoleNext && typeof window.NV_Shell === "function";
  return (
    <window.AuthGate>
      {next ? <window.NV_Shell /> : <window.SH_RootGate />}
    </window.AuthGate>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<AppRoot />);
