/* global React, Icon */

// ref:<slug>[#anchor] directive. Renders a clickable card that
// navigates to another doc. Body (optional) appears as an
// explanatory line. The lint engine (rule 2) catches broken refs at
// build time; this is rendered optimistically.

if (window.MarkdownDirectives) {
  window.MarkdownDirectives.register("ref:", ({ directive, body }) => {
    const target = directive.slice("ref:".length);
    const [slug, anchor] = target.split("#");
    const href = anchor ? `/docs/${slug}#${anchor}` : `/docs/${slug}`;
    const note = (body || "").trim();
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
          padding: "10px 14px",
          background: "var(--bg-2)",
          border: "1px solid var(--border)",
          borderRadius: 6,
          textDecoration: "none",
          margin: "12px 0",
          cursor: "pointer",
          color: "var(--text)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Icon name="doc" size={13} className="muted" />
          <span style={{ fontWeight: 600, fontSize: 13 }}>{slug}</span>
          {anchor && <span className="muted text-sm">#{anchor}</span>}
          <Icon name="chevron-right" size={11} className="muted" style={{ marginLeft: "auto" }} />
        </div>
        {note && (
          <div className="muted text-sm" style={{ marginTop: 6 }}>{note}</div>
        )}
      </a>
    );
  });
}
