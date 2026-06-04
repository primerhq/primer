/* global React */

// mermaid directive. Renders the body via window.mermaid.render. The
// library is lazy-loaded by DocsPage; until it's ready this shows a
// placeholder and re-renders when the global mermaid:loaded event
// fires.

function _MermaidBlock({ source }) {
  const id = React.useMemo(
    () => `mmd-${Math.random().toString(36).slice(2, 9)}`, [],
  );
  const [svg, setSvg] = React.useState(null);
  const [error, setError] = React.useState(null);

  const tryRender = React.useCallback(async () => {
    if (!window.mermaid) return;
    try {
      const result = await window.mermaid.render(id, source);
      setSvg(result.svg);
      setError(null);
    } catch (exc) {
      setError(String(exc && exc.message ? exc.message : exc));
    }
  }, [id, source]);

  React.useEffect(() => {
    tryRender();
    const onLoaded = () => tryRender();
    window.addEventListener("mermaid:loaded", onLoaded);
    return () => window.removeEventListener("mermaid:loaded", onLoaded);
  }, [tryRender]);

  if (error) {
    return (
      <div style={{
        padding: "10px 14px",
        margin: "12px 0",
        borderLeft: "3px solid var(--amber)",
        background: "var(--bg-2)",
        borderRadius: 4,
        color: "var(--amber)",
        fontSize: 12,
      }}>
        Mermaid syntax error: {error}
      </div>
    );
  }
  if (!svg) {
    return (
      <div className="muted text-sm" style={{ padding: "12px 0" }}>
        Loading diagram...
      </div>
    );
  }
  return (
    <div
      style={{ margin: "16px 0", textAlign: "center" }}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}

if (window.MarkdownDirectives) {
  window.MarkdownDirectives.register("mermaid", ({ body }) => (
    <_MermaidBlock source={body} />
  ));
}
