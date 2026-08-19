/* global React, SH_api, SH_useShell */
// One card, two mount points (spec section 8): inline in the transcript
// at the pause point, and as an attention item. Rendering it from ONE
// component over ONE item shape is what makes "rendered twice from one
// source" true rather than aspirational.
//
// Never a blocking modal: it is an ordinary section in the flow and it
// makes nothing behind it unreachable, so the user keeps scrolling the
// transcript and the trace while judging. v1 is approve or
// reject-with-feedback only; changing the call before approving it needs
// a backend contract and is a programme follow-up (C6).

function SH_DecisionCard(props) {
  var shell = SH_useShell();
  var item = props.item;
  var reasonState = React.useState("");
  var reason = reasonState[0];
  var setReason = reasonState[1];
  var answerState = React.useState("");
  var answer = answerState[0];
  var setAnswer = answerState[1];
  var busyState = React.useState(false);
  var busy = busyState[0];
  var setBusy = busyState[1];

  function settle(promise) {
    setBusy(true);
    return promise.then(function () {
      setBusy(false);
      if (props.onResolved) props.onResolved(item);
    }).catch(function (err) {
      setBusy(false);
      shell.toast("Decision failed: " + (err && err.message ? err.message : err));
    });
  }

  return (
    <section className="sh-decision" aria-modal="false"
      data-tier={item.tier} data-kind={item.kind}
      data-testid={"shell-decision:" + item.toolCallId}>
      <h4 className="sh-decision-title">{item.title}</h4>
      <pre className="sh-decision-preview" data-testid="shell-decision-preview">
        {item.preview}
      </pre>

      {item.kind === "question" ? (
        <div className="sh-decision-actions">
          <input type="text" data-testid="shell-decision-answer"
            value={answer} placeholder="Your answer"
            onChange={function (ev) { setAnswer(ev.target.value); }} />
          <button type="button" className="sh-verb" disabled={busy}
            onClick={function () {
              settle(SH_api.answer(item.sessionId, item.toolCallId, answer));
            }}>Send Answer</button>
        </div>
      ) : (
        <div className="sh-decision-actions">
          <button type="button" className="sh-verb"
            data-testid="shell-decision-approve" disabled={busy}
            onClick={function () {
              settle(SH_api.approve(item.sessionId, item.toolCallId));
            }}>Approve Gate</button>
          <input type="text" data-testid="shell-decision-reason"
            value={reason} placeholder="Why not? (sent to the agent)"
            onChange={function (ev) { setReason(ev.target.value); }} />
          <button type="button" className="sh-verb"
            data-testid="shell-decision-reject" disabled={busy}
            onClick={function () {
              settle(SH_api.reject(item.sessionId, item.toolCallId, reason));
            }}>Reject Gate</button>
        </div>
      )}
    </section>
  );
}

window.SH_DecisionCard = SH_DecisionCard;
