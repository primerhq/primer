// Minimal Python syntax highlighter.
// Pure function: takes source text + lang hint, returns an array of
// HTML strings (one per source line). Caller renders with whatever
// layout it wants (e.g. line-numbered, plain pre).
//
// First-party code; no upstream. Extracted from the inline highlighter
// in ui/components/workspaces.jsx (CodeHighlight) per Foundation
// Task 11, and the workspace file viewer's WS_CodeHighlight now calls
// this, so the file viewer's colouring stays consistent with chat.
// Kept under ui/vendor/ so the manifest catalogues every non-component
// asset; sha256 in ui/vendor/MANIFEST.md.
//
// One pass per line: a single alternation regex claims each region
// once, in the pass order the per-pass design used (comment, string,
// number, keyword), so a class can never match the span markup a class
// before it wrapped.

(function () {
  // Groups: 1 comment, 2 string, 3 number, 4 keyword.
  const PY_TOKEN =
    /(#.*$)|("""[\s\S]*?"""|".*?"|'.*?')|\b(\d+(?:\.\d+)?)\b|\b(import|from|def|class|return|if|else|elif|async|await|yield|in|not|and|or|with|as|for|while|try|except|finally|raise|pass|None|True|False)\b/g;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function span(color, text, italic) {
    return '<span style="color:' + color + (italic ? ";font-style:italic" : "") + '">' + text + '</span>';
  }

  // highlightPython(code, lang) -> Array<string>
  //   - lang === "python"  -> applies keyword colouring; others get
  //                          comment/string/number only (a keyword match
  //                          stays plain text, as when the keyword pass
  //                          did not run).
  //   - Empty lines map to "&nbsp;" so layouts that render each line
  //     in its own block don't visually collapse them.
  function highlightPython(code, lang) {
    const isPy = lang === "python";
    const lines = String(code || "").split("\n");
    return lines.map((line) => {
      const html = escapeHtml(line).replace(PY_TOKEN, function (m, comment, str, num) {
        if (comment !== undefined) return span("var(--text-4)", m, true);
        if (str !== undefined) return span("var(--green)", m);
        if (num !== undefined) return span("var(--amber)", m);
        return isPy ? span("var(--violet)", m) : m;
      });
      return html || "&nbsp;";
    });
  }

  window.primerVendor = window.primerVendor || {};
  window.primerVendor.highlightPython = highlightPython;
})();
