// Curl command tokeniser: colours `curl`, --flags, URLs, and quoted
// strings. Same shape as ui/vendor/highlight-python.js.

(function () {
  function escape(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function highlightCurl(src) {
    var out = escape(src);
    out = out.replace(
      /^(curl)\b/gm,
      '<span style="color: var(--violet); font-weight: 600">$1</span>',
    );
    out = out.replace(
      /(-[A-Z]\b|--[a-zA-Z][\w-]*)/g,
      '<span style="color: var(--blue)">$1</span>',
    );
    out = out.replace(
      /(https?:\/\/[^\s'"]+)/g,
      '<span style="color: var(--green)">$1</span>',
    );
    out = out.replace(
      /('[^']*'|"[^"]*")/g,
      '<span style="color: var(--amber)">$1</span>',
    );
    return out;
  }

  window.primerVendor = window.primerVendor || {};
  window.primerVendor.highlightCurl = highlightCurl;
})();
