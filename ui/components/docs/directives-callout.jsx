/* global React, Icon */

// callout:<kind> directive. Five styled boxes (info/success/warning/
// danger/tip), each with a coloured left band and an icon. Body is
// re-parsed as markdown so callouts can contain lists/links/inline
// code.

const _CALLOUT_STYLES = {
  info:    { color: "var(--blue)",   icon: "info" },
  success: { color: "var(--green)",  icon: "check-circle" },
  warning: { color: "var(--amber)",  icon: "alert" },
  danger:  { color: "var(--red)",    icon: "x-circle" },
  tip:     { color: "var(--violet)", icon: "zap" },
};

if (window.MarkdownDirectives) {
  window.MarkdownDirectives.register("callout:", ({ directive, body, renderMarkdown }) => {
    const kind = directive.slice("callout:".length);
    const style = _CALLOUT_STYLES[kind] || _CALLOUT_STYLES.info;
    return (
      <div style={{
        borderLeft: `3px solid ${style.color}`,
        background: "var(--bg-2)",
        padding: "10px 14px",
        margin: "12px 0",
        borderRadius: 4,
      }}>
        <div style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontWeight: 600,
          color: style.color,
          fontSize: 11,
          textTransform: "uppercase",
          letterSpacing: "0.04em",
          marginBottom: 6,
        }}>
          <Icon name={style.icon} size={12} />
          {kind}
        </div>
        <div className="md-body">
          {renderMarkdown(body)}
        </div>
      </div>
    );
  });
}
