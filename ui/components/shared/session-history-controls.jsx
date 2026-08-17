// Rewind, compact and structured-output controls for one session.
//
// PROPS ONLY, like the binding controls: no window.location, no ROUTES,
// no studio import, so the next shell re-hosts this rather than
// rewriting it.

function SessionHistoryControls({
  sessionId,
  workspaceId,
  sessionRow,
  onChanged,
  pushToast,
}) {
  const apiFetch = window.primerApi.apiFetch;
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState(null);
  const base = `/workspaces/${workspaceId}/sessions/${sessionId}`;
  const parked = !!(sessionRow && sessionRow.parked_status);

  // Both rejections below arrive as 409, and a single "conflict"
  // message would leave the user unable to tell which they hit. These
  // two are the ones they can act on, so they get their own text;
  // anything else is a toast.
  function explain(err) {
    const text = String((err && err.message) || err || "");
    if (text.indexOf("not idle") !== -1 || text.indexOf("in flight") !== -1) {
      return "This session is busy. Wait for the turn to finish.";
    }
    if (text.indexOf("compaction_marker") !== -1
        || text.indexOf("post-compaction") !== -1) {
      return "That point is inside compacted history. Pick a later message.";
    }
    return null;
  }

  async function call(path, body, okTitle) {
    if (busy) return null;
    setBusy(true);
    setNotice(null);
    try {
      const res = await apiFetch("POST", base + path, body || null);
      if (pushToast) pushToast({ kind: "info", title: okTitle });
      if (onChanged) onChanged(res);
      return res;
    } catch (err) {
      const known = explain(err);
      if (known) setNotice(known);
      else if (pushToast) {
        pushToast({
          kind: "error",
          title: "Action failed",
          detail: String((err && err.message) || err),
        });
      }
      return null;
    } finally {
      setBusy(false);
    }
  }

  // Abandoning a gate discards an agent's pending work, so it is never
  // a silent step on the way to a rewind. The user asks for it, sees
  // what it costs, and then rewinds as a second action.
  async function abandonGate() {
    await call("/cancel", null, "Gate abandoned");
  }

  async function rewindTo(toSeq) {
    await call("/rewind", { to_seq: toSeq }, "Rewound");
  }

  async function compactNow() {
    await call("/compact", null, "History compacted");
  }

  return (
    <div className="session-history-controls" data-testid="history-controls">
      {parked && (
        <div className="muted text-sm" data-testid="history-parked-note">
          This session is waiting on a gate.{" "}
          <button
            type="button"
            className="btn-ghost"
            data-testid="history-abandon-gate"
            disabled={busy}
            onClick={abandonGate}
          >
            Abandon it
          </button>{" "}
          to rewind, which discards the pending work.
        </div>
      )}

      <button
        type="button"
        className="btn-ghost"
        data-testid="history-compact"
        disabled={busy}
        onClick={compactNow}
        title="Summarise the conversation so far to shorten the prompt"
      >
        Compact
      </button>

      {notice && (
        <div className="muted text-sm" data-testid="history-notice">
          {notice}
        </div>
      )}

      {window.SchemaPanel && (
        <window.SchemaPanel
          sessionId={sessionId}
          workspaceId={workspaceId}
          value={sessionRow && sessionRow.response_format}
          onSave={async (schema) => {
            try {
              const res = await apiFetch(
                "PUT", base + "/response_format",
                { response_format: schema },
              );
              if (onChanged) onChanged(res);
              if (pushToast) {
                pushToast({ kind: "info", title: "Schema saved" });
              }
            } catch (err) {
              const known = explain(err);
              if (known) setNotice(known);
              else if (pushToast) {
                pushToast({
                  kind: "error",
                  title: "Schema rejected",
                  detail: String((err && err.message) || err),
                });
              }
            }
          }}
        />
      )}
    </div>
  );
}

// Exposed so a transcript row's "rewind to here" affordance can drive
// the same path the panel does, rather than re-implementing the call.
SessionHistoryControls.rewindPath = (workspaceId, sessionId) =>
  `/workspaces/${workspaceId}/sessions/${sessionId}/rewind`;

window.SessionHistoryControls = SessionHistoryControls;
