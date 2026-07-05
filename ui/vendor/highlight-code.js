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
// use — no new colours introduced.
//
// window.primerVendor.highlightCode(code, lang) -> string (HTML).
// Output is escaped-then-wrapped, never raw source markup, so callers
// may set it via dangerouslySetInnerHTML safely (see ui/vendor/markdown.jsx).

(function () {
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ----- js / ts / jsx -------------------------------------------------
  const JS_STRING = /(`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/g;
  const JS_COMMENT = /(\/\/.*$|\/\*[\s\S]*?\*\/)/gm;
  const JS_NUMBER = /\b(\d+(?:\.\d+)?)\b/g;
  const JS_KEYWORDS = /\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|class|extends|import|from|export|default|new|this|super|async|await|try|catch|finally|throw|typeof|instanceof|in|of|yield|true|false|null|undefined|interface|type|enum|implements|public|private|protected|readonly|as)\b/g;

  function highlightJsLike(escaped) {
    // Strings/comments first so their contents aren't re-coloured by
    // the keyword/number passes.
    let html = escaped;
    html = html.replace(JS_COMMENT, '<span style="color:var(--text-4)">$1</span>');
    html = html.replace(JS_STRING, '<span style="color:var(--green)">$1</span>');
    html = html.replace(JS_NUMBER, '<span style="color:var(--amber)">$1</span>');
    html = html.replace(JS_KEYWORDS, '<span style="color:var(--violet)">$1</span>');
    return html;
  }

  // ----- bash / sh -------------------------------------------------------
  const SH_STRING = /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/g;
  const SH_COMMENT = /(#.*$)/gm;
  const SH_FLAG = /(^|\s)(-{1,2}[a-zA-Z][\w-]*)/g;
  const SH_KEYWORDS = /\b(if|then|else|elif|fi|for|while|do|done|case|esac|function|return|export|local|in|set|echo)\b/g;

  function highlightShell(escaped) {
    let html = escaped;
    html = html.replace(SH_COMMENT, '<span style="color:var(--text-4)">$1</span>');
    html = html.replace(SH_STRING, '<span style="color:var(--green)">$1</span>');
    html = html.replace(SH_FLAG, '$1<span style="color:var(--blue)">$2</span>');
    html = html.replace(SH_KEYWORDS, '<span style="color:var(--violet)">$1</span>');
    return html;
  }

  // ----- generic fallback (unknown langs) --------------------------------
  const GEN_STRING = /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/g;
  const GEN_COMMENT = /(\/\/.*$|#.*$)/gm;
  const GEN_NUMBER = /\b(\d+(?:\.\d+)?)\b/g;
  const GEN_KEYWORDS = /\b(function|return|if|else|for|while|class|import|export|const|let|var|def|end|true|false|null|None|True|False)\b/g;

  function highlightGeneric(escaped) {
    let html = escaped;
    html = html.replace(GEN_COMMENT, '<span style="color:var(--text-4)">$1</span>');
    html = html.replace(GEN_STRING, '<span style="color:var(--green)">$1</span>');
    html = html.replace(GEN_NUMBER, '<span style="color:var(--amber)">$1</span>');
    html = html.replace(GEN_KEYWORDS, '<span style="color:var(--violet)">$1</span>');
    return html;
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
  //   js/ts/jsx, bash/sh -> hand-written tokenisers above.
  //   anything else -> generic comment/string/number/keyword pass.
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
