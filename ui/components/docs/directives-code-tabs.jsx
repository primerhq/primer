/* global React */

// code-tabs:<langs> directive. Splits the body on `--- <lang>` lines
// and renders a tabbed widget. Selected tab persists across page
// navigations in localStorage so an operator who prefers curl stays
// on curl across the docs.

const _CODE_TABS_STORAGE_KEY = "primer.docs.codeTabsLang";

function _CodeTabsRenderCode(code, lang) {
  const vendor = window.primerVendor || {};
  let html = null;
  if (lang === "python" && vendor.highlightPython) {
    html = vendor.highlightPython(code);
  } else if (lang === "json" && vendor.highlightJson) {
    html = vendor.highlightJson(code);
  } else if ((lang === "jsx" || lang === "javascript" || lang === "js" || lang === "ts" || lang === "tsx") && vendor.highlightJsx) {
    html = vendor.highlightJsx(code);
  } else if (lang === "curl" && vendor.highlightCurl) {
    html = vendor.highlightCurl(code);
  }
  if (html != null) {
    return <pre className={`md-pre lang-${lang}`}><code dangerouslySetInnerHTML={{ __html: html }} /></pre>;
  }
  return <pre className={`md-pre lang-${lang}`}><code>{code}</code></pre>;
}

function _CodeTabs({ langs, sections }) {
  const stored = (() => {
    try { return localStorage.getItem(_CODE_TABS_STORAGE_KEY); } catch (_e) { return null; }
  })();
  const initial = stored && langs.includes(stored) ? stored : langs[0];
  const [active, setActive] = React.useState(initial);
  const choose = (lang) => {
    setActive(lang);
    try { localStorage.setItem(_CODE_TABS_STORAGE_KEY, lang); } catch (_e) { /* noop */ }
  };
  return (
    <div style={{
      margin: "12px 0",
      border: "1px solid var(--border)",
      borderRadius: 6,
      overflow: "hidden",
    }}>
      <div style={{
        display: "flex",
        background: "var(--bg-2)",
        borderBottom: "1px solid var(--border)",
      }}>
        {langs.map((lang) => (
          <button
            key={lang}
            onClick={() => choose(lang)}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: active === lang ? "var(--bg)" : "transparent",
              border: "none",
              borderBottom: active === lang ? "2px solid var(--accent)" : "2px solid transparent",
              cursor: "pointer",
              color: active === lang ? "var(--text)" : "var(--text-3)",
              fontFamily: "var(--mono)",
            }}
          >
            {lang}
          </button>
        ))}
      </div>
      <div>
        {_CodeTabsRenderCode(sections[active] || "", active)}
      </div>
    </div>
  );
}

if (window.MarkdownDirectives) {
  window.MarkdownDirectives.register("code-tabs:", ({ directive, body }) => {
    const langs = directive.slice("code-tabs:".length).split(",").map((s) => s.trim()).filter(Boolean);
    const sections = {};
    let current = null;
    let buf = [];
    const flush = () => {
      if (current != null) sections[current] = buf.join("\n").replace(/^\n+|\n+$/g, "");
    };
    for (const line of body.split("\n")) {
      const m = line.match(/^---\s+(\w+)\s*$/);
      if (m) {
        flush();
        current = m[1];
        buf = [];
      } else if (current != null) {
        buf.push(line);
      }
    }
    flush();
    if (!langs.length) return null;
    return <_CodeTabs langs={langs} sections={sections} />;
  });
}
