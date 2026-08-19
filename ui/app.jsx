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
ReactDOM.createRoot(document.getElementById("root")).render(
  <window.AuthGate>
    <window.SH_RootGate />
  </window.AuthGate>
);
