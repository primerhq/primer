// Slim vendored code highlighter for chat markdown fenced code blocks.
// Pure function: takes source text + a lang hint, HTML-escapes it, then
// wraps a small token subset in <span style="color:var(--…)"> spans.
//
// First-party code; no upstream. Covers the slim language subset the
// chat surface actually emits: js/ts/jsx, bash/sh, json, python, plus a
// generic fallback (comments/strings/numbers/keywords) for anything
// else. json/python delegate to the existing highlight-json.js /
// highlight-python.js vendor files (loaded earlier in ui/index.html) so
// token colouring stays consistent across the console; js/ts/jsx and
// bash/sh get a small hand-written tokeniser here. Theme via the same
// var(--violet|green|amber|blue|text-4) tokens the other highlighters
// use - no new colours introduced.
//
// window.primerVendor.highlightCode(code, lang) -> string (HTML).
// Output is escaped-then-wrapped, never raw source markup, so callers
// may set it via dangerouslySetInnerHTML safely (see ui/vendor/markdown.jsx).
//
// One pass per language over the escaped source: a single alternation
// regex claims each region once, in the pass order the per-pass design
// used (comment, string, number, keyword), so a class can never match
// the span markup a class before it wrapped (the string pass used to
// wrap a "color:var(--...)" attribute, the number pass the 4 in
// --text-4, and the console showed the CSS fragments as text).

(function () {
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function span(color, text) {
    return '<span style="color:' + color + '">' + text + '</span>';
  }

  // ----- js / ts / jsx -------------------------------------------------
  // Groups: 1 comment, 2 string, 3 number, 4 keyword.
  const JS_TOKEN =
    /(\/\/.*$|\/\*[\s\S]*?\*\/)|(`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|\b(\d+(?:\.\d+)?)\b|\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|class|extends|import|from|export|default|new|this|super|async|await|try|catch|finally|throw|typeof|instanceof|in|of|yield|true|false|null|undefined|interface|type|enum|implements|public|private|protected|readonly|as)\b/gm;

  function highlightJsLike(escaped) {
    return escaped.replace(JS_TOKEN, function (m, comment, str, num) {
      if (comment !== undefined) return span("var(--text-4)", m);
      if (str !== undefined) return span("var(--green)", m);
      if (num !== undefined) return span("var(--amber)", m);
      return span("var(--violet)", m);
    });
  }

  // ----- bash / sh -------------------------------------------------------
  // Groups: 1 comment, 2 string, 3 flag leading whitespace, 4 flag, 5 keyword.
  const SH_TOKEN =
    /(#.*$)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(^|\s)(-{1,2}[a-zA-Z][\w-]*)|\b(if|then|else|elif|fi|for|while|do|done|case|esac|function|return|export|local|in|set|echo)\b/gm;

  function highlightShell(escaped) {
    return escaped.replace(SH_TOKEN, function (m, comment, str, ws, flag) {
      if (comment !== undefined) return span("var(--text-4)", m);
      if (str !== undefined) return span("var(--green)", m);
      if (flag !== undefined) return (ws || "") + span("var(--blue)", flag);
      return span("var(--violet)", m);
    });
  }

  // ----- generic fallback (unknown langs) --------------------------------
  // Groups: 1 comment, 2 string, 3 number, 4 keyword.
  const GEN_TOKEN =
    /(\/\/.*$|#.*$)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|\b(\d+(?:\.\d+)?)\b|\b(function|return|if|else|for|while|class|import|export|const|let|var|def|end|true|false|null|None|True|False)\b/gm;

  function highlightGeneric(escaped) {
    return escaped.replace(GEN_TOKEN, function (m, comment, str, num) {
      if (comment !== undefined) return span("var(--text-4)", m);
      if (str !== undefined) return span("var(--green)", m);
      if (num !== undefined) return span("var(--amber)", m);
      return span("var(--violet)", m);
    });
  }

  const JS_LANGS = { js: 1, jsx: 1, ts: 1, tsx: 1, javascript: 1, typescript: 1, mjs: 1, cjs: 1 };
  const SH_LANGS = { bash: 1, sh: 1, shell: 1, zsh: 1, console: 1 };

  // highlightCode(code, lang) -> string
  //   json  -> delegates to window.primerVendor.highlightJson (already
  //            escapes + colours the JSON token set).
  //   python -> delegates to window.primerVendor.highlightPython, which
  //            returns one HTML string per source line; rejoined with
  //            "\n" here so this function's contract stays a single
  //            HTML string like every other lang branch.
  //   js/ts/jsx, bash/sh -> single-pass tokenisers above.
  //   anything else -> single-pass generic comment/string/number/keyword.
  function highlightCode(code, lang) {
    const src = String(code == null ? "" : code);
    const norm = String(lang || "").toLowerCase();

    if (norm === "json" && window.primerVendor && window.primerVendor.highlightJson) {
      return window.primerVendor.highlightJson(src);
    }
    if ((norm === "python" || norm === "py") && window.primerVendor && window.primerVendor.highlightPython) {
      return window.primerVendor.highlightPython(src, "python").join("\n");
    }

    const escaped = escapeHtml(src);
    if (JS_LANGS[norm]) return highlightJsLike(escaped);
    if (SH_LANGS[norm]) return highlightShell(escaped);
    return highlightGeneric(escaped);
  }

  window.primerVendor = window.primerVendor || {};
  window.primerVendor.highlightCode = highlightCode;
})();
