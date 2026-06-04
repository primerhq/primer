// Minimal regex tokeniser for JSX / JS / TS code samples in docs.
// Returns escaped HTML with <span style="color: ..."> wrappers per
// token. Same shape as ui/vendor/highlight-python.js.

(function () {
  function escape(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  var KEYWORDS = /\b(const|let|var|function|return|if|else|for|while|class|extends|import|from|export|default|new|this|async|await|true|false|null|undefined|try|catch|finally|throw)\b/g;

  function highlightJsx(src) {
    var out = escape(src);
    // Strings (single, double, backtick) processed before keywords so
    // string content does not get keyword-coloured.
    out = out.replace(
      /(`(?:[^`\\]|\\.)*`|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g,
      '<span style="color: var(--green)">$1</span>',
    );
    out = out.replace(
      /(\/\/.*$)/gm,
      '<span style="color: var(--text-3)">$1</span>',
    );
    out = out.replace(
      KEYWORDS,
      '<span style="color: var(--violet)">$1</span>',
    );
    out = out.replace(
      /\b(\d+(?:\.\d+)?)\b/g,
      '<span style="color: var(--amber)">$1</span>',
    );
    return out;
  }

  window.primerVendor = window.primerVendor || {};
  window.primerVendor.highlightJsx = highlightJsx;
})();
