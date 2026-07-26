/* global React */
// Studio revamp - line diffing (ui/studio/STUDIO-WIRING.md §7).
//
// No library: the console ships no bundler, so a diff view either vendors
// another UMD blob into ui/vendor or computes its own. Line-level LCS is ~80
// lines and this is the whole requirement, so it computes its own.
//
// Two guards matter more than the algorithm, because both failure modes hang
// the browser tab rather than degrading:
//
//   1. Byte cap. A 5 MB generated file is not something anyone reads as a diff.
//   2. Cell cap. LCS is O(n*m); two 20k-line files are 400M cells. Common
//      prefix/suffix trimming usually collapses that to nothing, but a file
//      rewritten wholesale does not trim, so the DP itself needs a ceiling
//      beyond which the middle is reported as one replaced block.
//
// Pure logic, no React, so it can be exercised for real.

var ST2_DIFF_MAX_BYTES = 200 * 1024;
var ST2_DIFF_MAX_CELLS = 4000000;

function ST2_splitLines(text) {
  var s = String(text == null ? "" : text);
  if (s === "") return [];
  // A trailing newline denotes "file ends with a newline", not a final empty
  // line - otherwise every whole-file diff reports a phantom last line.
  if (s.charAt(s.length - 1) === "\n") s = s.slice(0, -1);
  return s.split("\n");
}

// ST2_diffLines(before, after) ->
//   { tooLarge: true, reason }                                   (capped)
// | { rows: [{ kind: "same"|"add"|"del", a, b, text }], stats: {added, removed} }
//
// `a` / `b` are 1-based line numbers in the before / after file, or null on the
// side where the line does not exist - which is what the gutter renders.
function ST2_diffLines(before, after) {
  var beforeStr = String(before == null ? "" : before);
  var afterStr = String(after == null ? "" : after);

  if (beforeStr.length > ST2_DIFF_MAX_BYTES || afterStr.length > ST2_DIFF_MAX_BYTES) {
    return {
      tooLarge: true,
      reason: "too large to diff here (over " + Math.round(ST2_DIFF_MAX_BYTES / 1024) + " KB)",
    };
  }

  var A = ST2_splitLines(beforeStr);
  var B = ST2_splitLines(afterStr);

  // Trim the common prefix / suffix. Almost every real edit is local, so this
  // is what keeps the DP small enough to run at all.
  var start = 0;
  while (start < A.length && start < B.length && A[start] === B[start]) start++;
  var endA = A.length;
  var endB = B.length;
  while (endA > start && endB > start && A[endA - 1] === B[endB - 1]) { endA--; endB--; }

  var midA = A.slice(start, endA);
  var midB = B.slice(start, endB);

  var rows = [];
  var added = 0;
  var removed = 0;

  for (var i = 0; i < start; i++) {
    rows.push({ kind: "same", a: i + 1, b: i + 1, text: A[i] });
  }

  if (midA.length * midB.length > ST2_DIFF_MAX_CELLS) {
    // Past the ceiling: report the changed middle as one replaced block rather
    // than freezing the tab. Honest about what it is - the counts stay exact.
    for (var d = 0; d < midA.length; d++) {
      rows.push({ kind: "del", a: start + d + 1, b: null, text: midA[d] });
      removed++;
    }
    for (var e = 0; e < midB.length; e++) {
      rows.push({ kind: "add", a: null, b: start + e + 1, text: midB[e] });
      added++;
    }
    for (var t = 0; t < A.length - endA; t++) {
      rows.push({ kind: "same", a: endA + t + 1, b: endB + t + 1, text: A[endA + t] });
    }
    return { rows: rows, stats: { added: added, removed: removed }, coarse: true };
  }

  // LCS length table over the trimmed middle.
  var n = midA.length;
  var m = midB.length;
  var lcs = [];
  for (var r = 0; r <= n; r++) {
    lcs.push(new Array(m + 1).fill(0));
  }
  for (var x = n - 1; x >= 0; x--) {
    for (var y = m - 1; y >= 0; y--) {
      lcs[x][y] = midA[x] === midB[y]
        ? lcs[x + 1][y + 1] + 1
        : Math.max(lcs[x + 1][y], lcs[x][y + 1]);
    }
  }

  // Walk it. Deletions before insertions at the same position, so a changed
  // line reads as del-then-add rather than interleaved.
  var p = 0;
  var q = 0;
  while (p < n && q < m) {
    if (midA[p] === midB[q]) {
      rows.push({ kind: "same", a: start + p + 1, b: start + q + 1, text: midA[p] });
      p++; q++;
    } else if (lcs[p + 1][q] >= lcs[p][q + 1]) {
      rows.push({ kind: "del", a: start + p + 1, b: null, text: midA[p] });
      removed++; p++;
    } else {
      rows.push({ kind: "add", a: null, b: start + q + 1, text: midB[q] });
      added++; q++;
    }
  }
  while (p < n) {
    rows.push({ kind: "del", a: start + p + 1, b: null, text: midA[p] });
    removed++; p++;
  }
  while (q < m) {
    rows.push({ kind: "add", a: null, b: start + q + 1, text: midB[q] });
    added++; q++;
  }

  for (var s2 = 0; s2 < A.length - endA; s2++) {
    rows.push({ kind: "same", a: endA + s2 + 1, b: endB + s2 + 1, text: A[endA + s2] });
  }

  return { rows: rows, stats: { added: added, removed: removed } };
}

// Collapse long unchanged stretches to a "@@ N unchanged lines" marker, the way
// every diff viewer does - a 2-line change in a 900-line file should not be 900
// rows of scrolling.
function ST2_collapseContext(rows, context) {
  var ctx = context == null ? 3 : context;
  var keep = {};
  (rows || []).forEach(function (row, i) {
    if (row.kind === "same") return;
    for (var j = Math.max(0, i - ctx); j <= Math.min(rows.length - 1, i + ctx); j++) {
      keep[j] = true;
    }
  });
  var out = [];
  var run = 0;
  (rows || []).forEach(function (row, i) {
    if (keep[i]) {
      if (run) { out.push({ kind: "gap", count: run }); run = 0; }
      out.push(row);
    } else {
      run++;
    }
  });
  if (run) out.push({ kind: "gap", count: run });
  return out;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

var ST2_DIFF_ROW_BG = {
  add: "var(--green-dim)",
  del: "var(--red-dim)",
  same: "transparent",
};

function ST2_DiffRow({ row }) {
  if (row.kind === "gap") {
    return (
      <div
        className="mono muted"
        data-testid="diff-gap"
        style={{
          padding: "2px 10px", fontSize: "var(--fs-11)",
          background: "var(--bg-1)", borderTop: "1px solid var(--bg-active)",
          borderBottom: "1px solid var(--bg-active)",
        }}
      >{"@@ " + row.count + " unchanged line" + (row.count === 1 ? "" : "s")}</div>
    );
  }
  var sign = row.kind === "add" ? "+" : row.kind === "del" ? "-" : " ";
  return (
    <div
      className="mono"
      data-testid={"diff-row-" + row.kind}
      style={{
        display: "flex", fontSize: "var(--fs-11)", lineHeight: 1.55,
        background: ST2_DIFF_ROW_BG[row.kind] || "transparent",
        whiteSpace: "pre",
      }}
    >
      <span style={{ width: 40, flex: "0 0 auto", textAlign: "right", paddingRight: 7, color: "var(--text-4)" }}>
        {row.a == null ? "" : row.a}
      </span>
      <span style={{ width: 40, flex: "0 0 auto", textAlign: "right", paddingRight: 7, color: "var(--text-4)" }}>
        {row.b == null ? "" : row.b}
      </span>
      <span style={{ width: 14, flex: "0 0 auto", color: "var(--text-3)" }}>{sign}</span>
      <span style={{ flex: 1, minWidth: 0, overflow: "auto" }}>{row.text}</span>
    </div>
  );
}

function DiffView({ before, after, path, context }) {
  var diff = React.useMemo(
    function () { return ST2_diffLines(before, after); },
    [before, after]
  );

  if (diff.tooLarge) {
    return (
      <div data-testid="diff-too-large" className="muted" style={{ padding: 14, fontSize: "var(--fs-12)" }}>
        {path ? path + ": " : ""}{diff.reason}
      </div>
    );
  }

  var rows = React.useMemo(
    function () { return ST2_collapseContext(diff.rows, context); },
    [diff, context]
  );

  return (
    <div data-testid="diff-view" className="col" style={{ gap: 0, minHeight: 0, overflow: "auto" }}>
      <div
        className="row"
        style={{
          padding: "5px 10px", gap: 8, alignItems: "center", flex: "0 0 auto",
          borderBottom: "1px solid var(--border)", background: "var(--bg-elev)",
          position: "sticky", top: 0, zIndex: 1,
        }}
      >
        {path ? <span className="mono" style={{ fontSize: "var(--fs-11)" }}>{path}</span> : null}
        <span style={{ marginLeft: "auto", display: "flex", gap: 7, fontSize: "var(--fs-11)" }}>
          <span style={{ color: "var(--green)" }} data-testid="diff-added">+{diff.stats.added}</span>
          <span style={{ color: "var(--red)" }} data-testid="diff-removed">-{diff.stats.removed}</span>
        </span>
      </div>
      {diff.coarse ? (
        <div className="muted" data-testid="diff-coarse" style={{ padding: "4px 10px", fontSize: "var(--fs-11)" }}>
          Rewritten wholesale - shown as a replaced block.
        </div>
      ) : null}
      {rows.map(function (row, i) {
        return <ST2_DiffRow key={i} row={row} />;
      })}
    </div>
  );
}

window.DiffView = DiffView;
window.ST2_diffLines = ST2_diffLines;
window.ST2_collapseContext = ST2_collapseContext;
window.ST2_splitLines = ST2_splitLines;
window.ST2_DIFF_MAX_BYTES = ST2_DIFF_MAX_BYTES;
window.ST2_DIFF_MAX_CELLS = ST2_DIFF_MAX_CELLS;
