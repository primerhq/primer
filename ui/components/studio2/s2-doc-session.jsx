/* global React */
// Native session documents: the workspace Studio's center panels
// (SessionAgentPanel / SessionGraphPanel, studio-center.jsx) re-housed
// under the Studio2 document contract. Reused, NOT forked - they carry
// the transcript stream, pause/resume/cancel controls, the composer
// (steer/inject surface), and the graph run view.
// Overrides the interim legacy-iframe registration from s2-legacy.jsx
// (this file loads after it; registerKind overwrites).

function S2_SessionDoc({ refId }) {
  const { useResource, apiFetch } = window.primerApi;
  const res = useResource(
    "studio2:session:" + refId,
    (signal) => apiFetch("GET", "/sessions/" + encodeURIComponent(refId), null, { signal }),
    { pollMs: 2000 },
  );
  const s = res.data;
  React.useEffect(() => {
    if (s && window.S2_Ctx) {
      window.S2_Ctx.noteActiveDoc("session", refId, s.workspace_id);
    }
  }, [refId, s && s.workspace_id]);
  if (!s) {
    return (
      <div style={{ padding: 16, color: "var(--text-3)",
        fontSize: "var(--fs-12)" }}>
        {res.error ? "Couldn't load this session. It may have been deleted."
          : "loading…"}
      </div>
    );
  }
  const isGraph =
    ((s.binding && s.binding.kind) || s.binding_kind) === "graph" ||
    !!(s.binding && s.binding.graph_id);
  const pushToast = window.primerApi.toastPush;
  const Panel = isGraph ? window.SessionGraphPanel : window.SessionAgentPanel;
  return (
    <div style={{ flex: 1, minHeight: 0, display: "flex",
      flexDirection: "column" }} data-testid="s2-session-doc">
      <Panel wid={s.workspace_id} sid={refId} session={s} pushToast={pushToast} />
    </div>
  );
}

window.S2_Docs.registerKind("session", {
  glyph: "▣",
  title: (ref) => ref,
  render: (ref) => <S2_SessionDoc refId={ref} />,
});
window.S2_SessionDoc = S2_SessionDoc;
