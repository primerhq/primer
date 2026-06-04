/* global React */

// mockup:<embed-id> directive. Looks up the id in window.DocsEmbeds
// and renders the component with body parsed as JSON props (empty
// body = no props). Unknown id shows an amber warning naming valid
// ids; malformed JSON shows a red warning with the parse error.

if (window.MarkdownDirectives) {
  window.MarkdownDirectives.register("mockup:", ({ directive, body }) => {
    const embedId = directive.slice("mockup:".length);
    const Component = (window.DocsEmbeds || {})[embedId];
    if (!Component) {
      const valid = (window.DocsEmbedIds && window.DocsEmbedIds()) || [];
      return (
        <div style={{
          padding: "10px 14px",
          margin: "12px 0",
          borderLeft: "3px solid var(--amber)",
          background: "var(--bg-2)",
          borderRadius: 4,
          color: "var(--text-2)",
          fontSize: 12,
        }}>
          Unknown mockup embed id: <code>{embedId}</code>.{" "}
          Valid ids: <code>{valid.join(", ")}</code>.
        </div>
      );
    }
    const trimmed = (body || "").trim();
    let props = {};
    if (trimmed) {
      try {
        props = JSON.parse(trimmed);
      } catch (exc) {
        return (
          <div style={{
            padding: "10px 14px",
            margin: "12px 0",
            borderLeft: "3px solid var(--red)",
            background: "var(--bg-2)",
            borderRadius: 4,
            color: "var(--red)",
            fontSize: 12,
          }}>
            mockup {embedId}: malformed JSON props: {String(exc.message || exc)}
          </div>
        );
      }
    }
    return (
      <div style={{
        margin: "16px 0",
        border: "1px dashed var(--border)",
        borderRadius: 6,
        padding: 12,
      }}>
        <div style={{
          fontSize: 10, color: "var(--text-3)",
          textTransform: "uppercase", letterSpacing: "0.06em",
          marginBottom: 8,
        }}>
          UI mockup &middot; {embedId}
        </div>
        <Component {...props} />
      </div>
    );
  });
}
