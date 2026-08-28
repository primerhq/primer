//
// parsePartialJson(text) -> { value, state }
//
// Best-effort JSON parser for the chat surface's live tool-argument
// rendering: the console streams tool-call arguments as the raw input
// text arrives (Vercel AI SDK tool-input-delta), so a half-formed
// argument object has to render *while it is still incomplete*. This
// takes the accumulated text buffer and returns the best object we can
// make of it, never throwing.
//
//   * state "complete"  - JSON.parse(text) succeeded verbatim.
//   * state "repaired"  - the text was incomplete; a repaired copy
//                         parsed and is returned.
//   * state "failed"    - nothing recoverable; value is undefined.
//
// Repair is a single linear scan (no regex, so no catastrophic
// backtracking): we walk the input tracking the open container stack,
// string state (including escapes), and the last safe cut point, then
// close the tail - close an open string, drop a trailing partial value
// (a number/literal cut mid-way, a dangling minus), drop a trailing
// comma, drop an incomplete object key (a key whose value never
// arrived), and close the open brackets/braces in stack order.
//
// Hand-written first-party code; no upstream is copied. See the
// sibling vendor files (highlight-json.js / markdown.jsx) for style.
//

(function () {
  "use strict";

  function isWhitespace(c) {
    return c === " " || c === "\t" || c === "\n" || c === "\r";
  }

  // A char that can appear inside a number or a bare literal (true/
  // false/null). Used only to know where a value token starts and ends
  // so a trailing partial literal can be dropped; it is not a JSON
  // grammar, just a boundary hint.
  function isValueChar(c) {
    return (c >= "0" && c <= "9") || c === "." || c === "-" || c === "+"
      || c === "e" || c === "E" || c === "t" || c === "r" || c === "u"
      || c === "f" || c === "a" || c === "l" || c === "s" || c === "n";
  }

  // A complete value token (number or literal) is one JSON.parse accepts
  // on its own; a cut ("1.2e", "tru", a lone "-") is not, so it is
  // dropped rather than kept as garbage.
  function isCompleteToken(tok) {
    try {
      JSON.parse(tok);
      return true;
    } catch (e) {
      return false;
    }
  }

  // Build a repaired string from a truncated/partial JSON prefix, or
  // return null when nothing recoverable can be made.
  function repair(text) {
    var s = String(text);
    var n = s.length;
    if (n === 0) return null;

    // Open container frames. Each frame records the last safe cut point
    // (after the last complete value, or the container-open position) and,
    // for objects, a pending key whose value never completed.
    var frames = []; // { c, safe, keyStart, pendingKey, afterColon }
    var inString = false;
    var escape = false;
    var escapeStart = -1; // index of the last backslash seen inside a string
    var openIsKey = false; // the open string is an object key
    var valueStart = -1; // start of the current number/literal token
    var inValue = false;
    var topLevelSafe = 0;

    var i = 0;
    while (i < n) {
      var c = s[i];

      if (inValue && !isValueChar(c)) {
        var doneTok = s.slice(valueStart, i);
        if (isCompleteToken(doneTok)) {
          var df = frames[frames.length - 1];
          if (df) {
            df.safe = i;
            df.pendingKey = false;
            df.keyStart = -1;
            df.afterColon = false;
          } else {
            topLevelSafe = i;
          }
        }
        inValue = false;
        valueStart = -1;
      }

      if (isWhitespace(c)) { i++; continue; }

      if (inString) {
        if (escape) escape = false;
        else if (c === "\\") {
          escape = true;
          escapeStart = i;
        } else if (c === '"') {
          inString = false;
          // A complete string value ends here.
          if (openIsKey) {
            // The string was a key; the colon/value follow. The key is
            // done but its value is still pending, so the frame's safe
            // point stays before the key (cutting the whole pair if the
            // value never arrives).
            var kf = frames[frames.length - 1];
            if (kf) kf.pendingKey = true;
          } else {
            // A value string (or top-level). Mark the cut point after it.
            var vf = frames[frames.length - 1];
            if (vf) {
              vf.safe = i + 1;
              vf.pendingKey = false;
              vf.keyStart = -1;
              vf.afterColon = false;
            } else {
              topLevelSafe = i + 1;
            }
          }
        }
        i++;
        continue;
      }

      if (c === '"') {
        inString = true;
        inValue = false;
        valueStart = -1;
        var f = frames[frames.length - 1];
        // A string in an object before any colon is a key.
        if (f && f.c === "{" && !f.afterColon) {
          openIsKey = true;
          f.keyStart = i;
          f.pendingKey = true;
        } else {
          openIsKey = false;
        }
        i++;
        continue;
      }

      if (c === "[" || c === "{") {
        frames.push({
          c: c, safe: i + 1, keyStart: -1,
          pendingKey: false, afterColon: false,
        });
        inValue = false;
        valueStart = -1;
        i++;
        continue;
      }

      if (c === "]") {
        var arr = frames[frames.length - 1];
        if (arr && arr.c === "[") {
          frames.pop();
          var p1 = frames[frames.length - 1];
          if (p1) {
            p1.safe = i + 1;
            p1.pendingKey = false;
            p1.keyStart = -1;
            p1.afterColon = false;
          } else {
            topLevelSafe = i + 1;
          }
        }
        inValue = false;
        valueStart = -1;
        i++;
        continue;
      }

      if (c === "}") {
        var obj = frames[frames.length - 1];
        if (obj && obj.c === "{") {
          frames.pop();
          var p2 = frames[frames.length - 1];
          if (p2) {
            p2.safe = i + 1;
            p2.pendingKey = false;
            p2.keyStart = -1;
            p2.afterColon = false;
          } else {
            topLevelSafe = i + 1;
          }
        }
        inValue = false;
        valueStart = -1;
        i++;
        continue;
      }

      if (c === ":") {
        var cf = frames[frames.length - 1];
        if (cf && cf.c === "{") cf.afterColon = true;
        i++;
        continue;
      }

      if (c === ",") {
        // The value before the comma is complete; the cut point is the
        // last complete value, so a trailing comma is naturally excluded
        // when we cut. Nothing to record.
        inValue = false;
        valueStart = -1;
        i++;
        continue;
      }

      // A number/literal token char, or a stray char that ends a token.
      if (isValueChar(c)) {
        if (!inValue) {
          valueStart = i;
          inValue = true;
        }
        i++;
        continue;
      }

      // A char that is not part of any value token ends the current
      // token (whitespace/commas/closers are handled above; anything
      // else is a structural error - stop building the value).
      inValue = false;
      valueStart = -1;
      i++;
    }

    // --- Build the repaired string from the walk state. ---

    var cut = n; // the index up to which we keep the input
    var closeString = false;

    if (inString) {
      if (escapeStart !== -1) {
        var eLen = n - escapeStart;
        var isU = (s[escapeStart + 1] === "u" || s[escapeStart + 1] === "U");
        if ((isU && eLen < 6) || (!isU && eLen === 1)) {
          cut = Math.min(cut, escapeStart);
        }
      }
      if (openIsKey) {
        // An open key with no completed value: drop the whole pair.
        cut = frames.length ? frames[frames.length - 1].safe : topLevelSafe;
      } else {
        // An open value string: close it so it becomes a complete value.
        closeString = true;
      }
    } else if (inValue) {
      // A trailing number/literal token: keep it only if complete.
      var tok = s.slice(valueStart);
      if (!isCompleteToken(tok)) cut = frames.length ? frames[frames.length - 1].safe : topLevelSafe;
    } else {
      // No open value, but an object may have a dangling key (a colon
      // whose value never arrived). Drop that pair.
      var top = frames[frames.length - 1];
      if (top && top.c === "{" && top.afterColon) cut = top.safe;
    }

    // Drop a trailing comma / whitespace so the close is valid.
    var body = s.slice(0, cut);
    while (body.length) {
      var last = body.charAt(body.length - 1);
      if (last === "," || isWhitespace(last)) {
        body = body.slice(0, body.length - 1);
      } else {
        break;
      }
    }

    if (closeString) body = body + '"';

    // Close the open containers in stack order (innermost first).
    for (var k = frames.length - 1; k >= 0; k--) {
      body = body + (frames[k].c === "[" ? "]" : "}");
    }

    if (body.length === 0) return null;
    return body;
  }

  // parsePartialJson(text) -> { value, state }
  function parsePartialJson(text) {
    if (typeof text !== "string") return { value: undefined, state: "failed" };

    // 1. Fast path: a verbatim parse is the whole job.
    try {
      var v = JSON.parse(text);
      return { value: v, state: "complete" };
    } catch (e) {
      // 2. The prefix is incomplete; repair the tail and re-parse.
    }

    var repaired = repair(text);
    if (repaired === null) return { value: undefined, state: "failed" };
    try {
      var v2 = JSON.parse(repaired);
      return { value: v2, state: "repaired" };
    } catch (e2) {
      return { value: undefined, state: "failed" };
    }
  }

  window.parsePartialJson = parsePartialJson;
})();
