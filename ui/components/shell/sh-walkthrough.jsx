/* global React, SH_useShell */
// The checklist that rides the seeded operator turn. Each row RUNS its
// verb; none of them describes one.

function SH_Walkthrough(props) {
  var shell = SH_useShell();
  var doneState = React.useState(function () {
    try {
      var raw = window.localStorage.getItem(
        window.SH_walkthroughDoneKey(shell.username)
      );
      return raw ? JSON.parse(raw) : {};
    } catch (_e) { return {}; }
  });
  var done = doneState[0];
  var setDone = doneState[1];

  function mark(id) {
    var next = Object.assign({}, done);
    next[id] = true;
    setDone(next);
    try {
      window.localStorage.setItem(
        window.SH_walkthroughDoneKey(shell.username), JSON.stringify(next)
      );
    } catch (_e) { /* best effort */ }
  }

  return (
    <ol className="sh-walkthrough" data-testid="shell-walkthrough">
      {window.SH_WALKTHROUGH_STEPS.map(function (step) {
        var verb = shell.registry.get(step.verbId);
        return (
          <li key={step.id} data-done={!!done[step.id]}
            data-testid={"shell-walkthrough-step:" + step.id}>
            <button type="button" className="sh-verb" disabled={!verb}
              onClick={function () {
                mark(step.id);
                if (verb) verb.run();
              }}>
              {step.label}
              {verb && verb.chord ? (
                <span className="sh-chord">{verb.chord}</span>
              ) : null}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

window.SH_Walkthrough = SH_Walkthrough;
