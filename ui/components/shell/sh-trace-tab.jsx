/* global React, SH_useShell */
// The Trace tab (spec section 4, S7; pinned decision 8).
//
// A HOST WRAPPER, not a renderer. S7 P4 Task 20 ships
// window.SessionTracePanel({sid, turnNo, sessionStatus}) in
// ui/components/shared/session-trace.jsx, built props-only (no route table, no
// window.location) expressly so this shell can re-host it unchanged. S7
// lands before S8, and S7's only other mount (studio-center.jsx) dies at
// the flag day, so re-hosting here is what keeps that component alive and
// keeps ONE tree renderer in the tree.
//
// What stays ours: the `trace` doc kind, the ref grammar below, and the
// split-group plumbing in sh-doc-host.jsx. The node tree, the raw-argument
// expander and the timeline fetch are the panel's.
//
// This tab is the exhaustive record. Raw arguments appear here and
// nowhere else, which is precisely what lets the transcript's tool chips
// stay plain language.

function SH_traceRef(sid, turnNo) {
  return String(sid) + ":" + String(turnNo);
}

function SH_parseTraceRef(ref) {
  var text = String(ref == null ? "" : ref);
  var cut = text.lastIndexOf(":");
  if (cut <= 0) return null;
  var turn = parseInt(text.slice(cut + 1), 10);
  if (!(turn >= 0)) return null;
  return { sid: text.slice(0, cut), turnNo: turn };
}

// docRef, not ref: React reserves `ref`, so a prop spelled that way is
// swallowed by the element and never reaches the component.
//
// The whole body is: parse the ref, look up the session status the shell
// already holds, hand both to the S7 panel. Nothing else belongs here.
function SH_TraceTab(props) {
  var shell = SH_useShell();
  var parsed = SH_parseTraceRef(props.docRef);
  if (!parsed) {
    return <div className="sh-trace-empty">No turn selected.</div>;
  }
  return (
    <div className="sh-trace"
      data-testid={"shell-trace:" + parsed.sid + ":" + parsed.turnNo}>
      <window.SessionTracePanel
        sid={parsed.sid}
        turnNo={parsed.turnNo}
        sessionStatus={shell.sessionStatus(parsed.sid)}
      />
    </div>
  );
}
