// Minimal JSON syntax highlighter.
// Pure function: takes a JSON string (typically the result of
// JSON.stringify(obj, null, 2)) and returns an HTML string with
// per-token <span> wrapping. Caller is responsible for setting
// dangerouslySetInnerHTML and wrapping in <pre>/<code> for whitespace.
//
// First-party code; no upstream. Written for Foundation Task 11 to
// back the future RFC 7807 envelope renderer in session-detail.jsx
// once Sessions sub-project P2 swaps the bespoke inline JSX in
// session-detail.jsx for live error data. The bespoke inline render
// (which hand-laid every known field as JSX spans) stays untouched
// until P2 since it depends on mock data shape.

(function () {
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // highlightJson(jsonText) -> string
  //   Tokens: object keys (string + trailing colon), string values,
  //   numbers, booleans, null. Punctuation and whitespace pass through.
  //   Distinguishes keys (blue) from string values (green) by the
  //   trailing colon — same trick most editors use.
  function highlightJson(jsonText) {
    const escaped = escapeHtml(String(jsonText));
    return escaped.replace(
      /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
      function (match, str, colon, kw, num) {
        if (str) {
          if (colon) return '<span style="color:var(--blue)">' + str + '</span>' + colon;
          return '<span style="color:var(--green)">' + str + '</span>';
        }
        if (kw) return '<span style="color:var(--violet)">' + kw + '</span>';
        if (num) return '<span style="color:var(--amber)">' + num + '</span>';
        return match;
      }
    );
  }

  window.primerVendor = window.primerVendor || {};
  window.primerVendor.highlightJson = highlightJson;
})();
