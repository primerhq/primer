// Minimal Python syntax highlighter.
// Pure function: takes source text + lang hint, returns an array of
// HTML strings (one per source line). Caller renders with whatever
// layout it wants (e.g. line-numbered, plain pre).
//
// First-party code; no upstream. Extracted from the inline highlighter
// in ui/components/workspaces.jsx (CodeHighlight) per Foundation
// Task 11. Kept under ui/vendor/ so the manifest catalogues every
// non-component asset; sha256 in ui/vendor/MANIFEST.md.

(function () {
  const KEYWORDS = /\b(import|from|def|class|return|if|else|elif|async|await|yield|in|not|and|or|with|as|for|while|try|except|finally|raise|pass|None|True|False)\b/g;
  const COMMENT = /(#.*$)/g;
  const STRING = /("""[\s\S]*?"""|".*?"|'.*?')/g;
  const NUMBER = /\b(\d+(?:\.\d+)?)\b/g;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // highlightPython(code, lang) -> Array<string>
  //   - lang === "python"  → applies keyword colouring; others get
  //                          comment/string/number only.
  //   - Empty lines map to "&nbsp;" so layouts that render each line
  //     in its own block don't visually collapse them.
  function highlightPython(code, lang) {
    const isPy = lang === "python";
    const lines = String(code || "").split("\n");
    return lines.map((line) => {
      let html = escapeHtml(line);
      html = html.replace(COMMENT, '<span style="color:var(--text-4);font-style:italic">$1</span>');
      html = html.replace(STRING, '<span style="color:var(--green)">$1</span>');
      html = html.replace(NUMBER, '<span style="color:var(--amber)">$1</span>');
      if (isPy) {
        html = html.replace(KEYWORDS, '<span style="color:var(--violet)">$1</span>');
      }
      return html || "&nbsp;";
    });
  }

  window.primerVendor = window.primerVendor || {};
  window.primerVendor.highlightPython = highlightPython;
})();
