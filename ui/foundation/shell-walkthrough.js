// primer UI - the first-run walkthrough (S8 spec section 8).
//
// There is no welcome page. The walkthrough IS the operator session: S5
// seeds one operator turn, and this checklist rides on it. Every step is
// a LIVE VERB INVOCATION, identified by registry id, so a renamed verb
// breaks the test rather than the first run.
//
// Four steps. Short on purpose: onboarding sprawl is an antipattern.

var SH_WALKTHROUGH_STEPS = [
  { id: "palette", label: "Open the Palette", verbId: "doc.openQuick" },
  { id: "file", label: "Open a File", verbId: "doc.openQuick" },
  { id: "attention", label: "Open Attention", verbId: "attention.next" },
  { id: "split", label: "Split Right", verbId: "layout.splitRight" },
];

function SH_walkthroughDoneKey(username) {
  return "primer.shell.walkthrough:" + String(username || "anon");
}

// Active while the seeded turn is present. Derived from the transcript
// rather than a local flag, so a second browser sees the same state.
function SH_walkthroughState(rows) {
  var active = false;
  for (var i = 0; i < (rows || []).length; i++) {
    var payload = rows[i].payload || {};
    if (payload.walkthrough_seed) { active = true; break; }
  }
  return { active: active, doneIds: [] };
}

window.SH_WALKTHROUGH_STEPS = SH_WALKTHROUGH_STEPS;
window.SH_walkthroughDoneKey = SH_walkthroughDoneKey;
window.SH_walkthroughState = SH_walkthroughState;
