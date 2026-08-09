// ExternalPendingBanner — pending invoker-supplied (external) tool calls
// for one session or chat. Read-only + cancel: responding is the API
// caller's job (tool_results on the invocation endpoint), so the console
// only shows what the conversation is waiting on and lets an operator
// abort a stuck call. Polls GET .../external_tools/pending (5s).
/* global React */

(function () {
  const { useResource, apiFetch } = window.primerApi;

  function ExternalPendingBanner({ sessionId, chatId, pushToast }) {
    const owner = sessionId || chatId;
    const base = sessionId ? `/sessions/${sessionId}` : `/chats/${chatId}`;
    const res = useResource(
      `external-pending:${owner}`,
      (signal) =>
        apiFetch("GET", `${base}/external_tools/pending`, null, { signal }),
      { pollMs: 5000 },
    );
    const items = (res.data && res.data.items) || [];
    if (!items.length) return null;

    async function cancel(tcid) {
      try {
        // Sessions expose the yield-cancel endpoint; chat pendings are
        // superseded by the next message, so no chat-side cancel here.
        await apiFetch("POST", `/sessions/${sessionId}/yields/${tcid}/cancel`, {
          reason: "cancelled from console",
        });
        pushToast && pushToast({ kind: "warning", title: `Cancelled ${tcid}` });
        if (res.refetch) res.refetch();
      } catch (err) {
        pushToast &&
          pushToast({
            kind: "error",
            title: "Cancel failed",
            detail: (err && err.detail) || (err && err.message),
          });
      }
    }

    return (
      <div
        data-testid="external-pending"
        style={{
          border: "1px solid var(--warning, var(--border))",
          borderRadius: 6,
          padding: "8px 12px",
          margin: "8px 12px",
          display: "flex",
          flexDirection: "column",
          gap: 6,
          flex: "0 0 auto",
        }}
      >
        <div className="mono" style={{ fontSize: 12, fontWeight: 600, color: "var(--text)" }}>
          Waiting on external tool call{items.length > 1 ? "s" : ""}
        </div>
        {items.map((it) => (
          <div
            key={it.tool_call_id}
            style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}
          >
            <code className="mono" style={{ fontSize: 12 }}>{it.tool_name}</code>
            {it.node_id ? (
              <span className="mono" style={{ fontSize: 11, color: "var(--text-4)" }}>
                node {it.node_id}
              </span>
            ) : null}
            <span
              className="mono"
              style={{
                fontSize: 11,
                color: "var(--text-4)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                flex: 1,
                minWidth: 0,
              }}
            >
              {JSON.stringify(it.arguments || {}).slice(0, 120)}
            </span>
            {sessionId && window.Btn ? (
              <window.Btn
                size="sm"
                kind="ghost"
                icon="x"
                onClick={() => cancel(it.tool_call_id)}
                title="Cancel this pending external tool call"
              >Cancel</window.Btn>
            ) : null}
          </div>
        ))}
        <div style={{ fontSize: 11, color: "var(--text-4)" }}>
          The invoking application must respond through the invocation API.
        </div>
      </div>
    );
  }

  window.ExternalPendingBanner = ExternalPendingBanner;
})();
