/* global React, Icon */

// ai-doc:<slug> directive. Renders a violet-banded card linking to
// the AI-doc mirror at /docs/_ai/<slug>. Used by operator docs to
// cross-link to the dense MCP-tool-focused agent reference.

if (window.MarkdownDirectives) {
  window.MarkdownDirectives.register("ai-doc:", ({ directive }) => {
    const slug = directive.slice("ai-doc:".length);
    const href = `/docs/_ai/${slug}`;
    const onClick = (e) => {
      e.preventDefault();
      const router = window.primerApi && window.primerApi.useRouter
        ? window.primerApi.useRouter()
        : null;
      if (router && router.navigate) {
        router.navigate(href);
      } else {
        window.location.hash = `#${href}`;
      }
    };
    return (
      <a
        href={`#${href}`}
        onClick={onClick}
        style={{
          display: "block",
          padding: "12px 14px",
          background: "var(--bg-2)",
          border: "1px solid var(--violet)",
          borderLeft: "4px solid var(--violet)",
          borderRadius: 6,
          textDecoration: "none",
          margin: "16px 0",
          cursor: "pointer",
          color: "var(--text)",
        }}
      >
        <div style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontWeight: 600,
          fontSize: 11,
          color: "var(--violet)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          marginBottom: 6,
        }}>
          <Icon name="agent" size={12} />
          Agent-facing reference
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontWeight: 600, fontSize: 13 }}>{slug}</span>
          <Icon name="chevron-right" size={11} className="muted" style={{ marginLeft: "auto" }} />
        </div>
      </a>
    );
  });
}
