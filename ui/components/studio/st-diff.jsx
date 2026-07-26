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

// ---------------------------------------------------------------------------
// The other producer: git's own unified diff
//
// ST2_diffLines computes a diff when the caller holds BOTH sides. For history
// that is not the case, and cannot be: /files/read serves the working tree, so
// there is no way to fetch "the file as it was at commit N-1". What IS
// available is the unified patch git already computed, per file, from
// GET /v1/workspaces/{wid}/commit/{sha}.
//
// So this parses that instead of reconstructing the two sides. It is also
// strictly better than reconstructing: the counts and hunk boundaries are
// git's, not an approximation of git's, and it costs one request rather than
// two-plus-a-guess. Both producers emit the SAME row shape, so DiffView renders
// either without knowing which it got.
// ---------------------------------------------------------------------------

var ST2_HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

// ST2_parseUnifiedDiff(patch) ->
//   { rows, stats: {added, removed}, binary?: true }
function ST2_parseUnifiedDiff(patch) {
  var text = String(patch == null ? "" : patch);
  if (!text) return { rows: [], stats: { added: 0, removed: 0 } };

  var rows = [];
  var added = 0;
  var removed = 0;
  var oldLine = 0;
  var newLine = 0;
  var inHunk = false;
  // Drop ONE trailing newline before splitting. Otherwise the final "\n"
  // yields an empty last element, which reads as a blank context line and
  // advances both line counters - so every row after a patch that ends
  // normally would be numbered one too high. An INTERIOR "" is a real blank
  // context line and must survive, which is why this trims rather than filters.
  if (text.charAt(text.length - 1) === "\n") text = text.slice(0, -1);
  var lines = text.split("\n");

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];

    // git reports a non-text blob instead of hunks.
    if (!inHunk && line.indexOf("Binary files") === 0) {
      return { rows: [], stats: { added: 0, removed: 0 }, binary: true };
    }

    var hunk = ST2_HUNK_RE.exec(line);
    if (hunk) {
      // A gap between hunks is unchanged context git chose not to send. Say
      // how far it jumped rather than silently butting two hunks together.
      if (inHunk) rows.push({ kind: "gap", count: null });
      oldLine = parseInt(hunk[1], 10);
      newLine = parseInt(hunk[3], 10);
      inHunk = true;
      continue;
    }
    if (!inHunk) continue; // diff --git / index / --- / +++ preamble

    var head = line.charAt(0);
    if (head === "\\") continue; // "\ No newline at end of file"
    if (head === "+") {
      rows.push({ kind: "add", a: null, b: newLine, text: line.slice(1) });
      newLine++; added++;
    } else if (head === "-") {
      rows.push({ kind: "del", a: oldLine, b: null, text: line.slice(1) });
      oldLine++; removed++;
    } else if (head === " " || line === "") {
      // A fully-empty context line arrives as "" rather than " ".
      rows.push({ kind: "same", a: oldLine, b: newLine, text: line.slice(1) });
      oldLine++; newLine++;
    } else {
      // Anything else means the hunk ended (a trailing "diff --git" for the
      // next file, say). Stop rather than mis-numbering the rest.
      inHunk = false;
    }
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
    // count is null for a hunk boundary in a git patch: the unchanged run was
    // never sent, so its length is genuinely unknown. Do not print "null".
    var label = row.count == null
      ? "@@ unchanged lines"
      : "@@ " + row.count + " unchanged line" + (row.count === 1 ? "" : "s");
    return (
      <div
        className="mono muted"
        data-testid="diff-gap"
        style={{
          padding: "2px 10px", fontSize: "var(--fs-11)",
          background: "var(--bg-1)", borderTop: "1px solid var(--bg-active)",
          borderBottom: "1px solid var(--bg-active)",
        }}
      >{label}</div>
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

// Side-by-side needs both gutters plus two text columns; below this the columns
// are too narrow to read and unified wins. Switched on measured width, never a
// user toggle (WIRING §7.3) - the right answer is a property of the container,
// so making the reader choose is making them do the layout's job.
var ST2_SIDE_BY_SIDE_MIN_W = 900;

// Past this many rows the DOM cost shows. Rather than a virtualiser (which
// needs fixed row heights the diff does not have), cap and say so: a reader who
// needs line 3000 of a diff wants the file, not this pane.
var ST2_DIFF_ROW_CAP = 500;

// Pair rows into {left, right} for the side-by-side layout: a del/add run
// becomes one row per pair so a changed line sits opposite its replacement.
function ST2_pairRows(rows) {
  var out = [];
  var i = 0;
  while (i < rows.length) {
    var row = rows[i];
    if (row.kind === "same" || row.kind === "gap") {
      out.push({ left: row, right: row, both: true });
      i++;
      continue;
    }
    // Collect the contiguous del run, then the contiguous add run, and zip.
    var dels = [];
    var adds = [];
    while (i < rows.length && rows[i].kind === "del") { dels.push(rows[i]); i++; }
    while (i < rows.length && rows[i].kind === "add") { adds.push(rows[i]); i++; }
    var n = Math.max(dels.length, adds.length);
    for (var j = 0; j < n; j++) {
      out.push({ left: dels[j] || null, right: adds[j] || null, both: false });
    }
  }
  return out;
}

function ST2_SideCell({ row, side }) {
  var bg = "transparent";
  if (row) bg = ST2_DIFF_ROW_BG[row.kind] || "transparent";
  var num = row ? (side === "left" ? row.a : row.b) : null;
  return (
    <div
      className="mono"
      style={{
        display: "flex", flex: "1 1 50%", minWidth: 0,
        fontSize: "var(--fs-11)", lineHeight: 1.55, whiteSpace: "pre",
        background: row ? bg : "var(--bg-1)",
      }}
    >
      <span style={{ width: 40, flex: "0 0 auto", textAlign: "right", paddingRight: 7, color: "var(--text-4)" }}>
        {num == null ? "" : num}
      </span>
      <span style={{ flex: 1, minWidth: 0, overflow: "hidden" }}>{row ? row.text : ""}</span>
    </div>
  );
}

// DiffView takes EITHER a git patch (history: git already did the diff) or a
// before/after pair (a dirty buffer against saved content). One renderer, two
// producers - see the header comment on ST2_parseUnifiedDiff.
function DiffView({ before, after, patch, path, context }) {
  var hostRef = React.useRef(null);
  var wState = React.useState(0);
  var width = wState[0];
  var setWidth = wState[1];

  React.useEffect(function () {
    var el = hostRef.current;
    if (!el || typeof window.ResizeObserver !== "function") return undefined;
    var ro = new window.ResizeObserver(function (entries) {
      if (entries && entries[0]) setWidth(entries[0].contentRect.width);
    });
    ro.observe(el);
    setWidth(el.clientWidth);
    return function () { ro.disconnect(); };
  }, []);

  var diff = React.useMemo(function () {
    return patch != null
      ? ST2_parseUnifiedDiff(patch)
      : ST2_diffLines(before, after);
  }, [patch, before, after]);

  var rows = React.useMemo(function () {
    // A parsed patch already omits its unchanged runs, so re-collapsing it
    // would only re-collapse git's own context lines.
    return patch != null ? diff.rows : ST2_collapseContext(diff.rows, context);
  }, [diff, patch, context]);

  if (diff.tooLarge) {
    return (
      <div data-testid="diff-too-large" className="muted" style={{ padding: 14, fontSize: "var(--fs-12)" }}>
        {path ? path + ": " : ""}{diff.reason}
      </div>
    );
  }
  if (diff.binary) {
    return (
      <div data-testid="diff-binary" className="muted" style={{ padding: 14, fontSize: "var(--fs-12)" }}>
        {path ? path + ": " : ""}binary file - no line diff to show.
      </div>
    );
  }

  var capped = rows.length > ST2_DIFF_ROW_CAP;
  var shown = capped ? rows.slice(0, ST2_DIFF_ROW_CAP) : rows;
  var wide = width >= ST2_SIDE_BY_SIDE_MIN_W;
  var paired = React.useMemo(
    function () { return wide ? ST2_pairRows(shown) : null; },
    [wide, shown]
  );

  return (
    <div
      ref={hostRef}
      data-testid="diff-view"
      data-layout={wide ? "side-by-side" : "unified"}
      className="col"
      style={{ gap: 0, minHeight: 0, overflow: "auto" }}
    >
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

      {wide
        ? paired.map(function (pair, i) {
            if (pair.both && pair.left.kind === "gap") {
              return <ST2_DiffRow key={i} row={pair.left} />;
            }
            return (
              <div key={i} data-testid="diff-pair" style={{ display: "flex", minWidth: 0 }}>
                <ST2_SideCell row={pair.left} side="left" />
                <span style={{ width: 1, flex: "0 0 auto", background: "var(--border)" }} />
                <ST2_SideCell row={pair.right} side="right" />
              </div>
            );
          })
        : shown.map(function (row, i) {
            return <ST2_DiffRow key={i} row={row} />;
          })}

      {capped ? (
        <div
          data-testid="diff-truncated"
          className="muted"
          style={{ padding: "8px 10px", fontSize: "var(--fs-11)", borderTop: "1px solid var(--border)" }}
        >
          Showing the first {ST2_DIFF_ROW_CAP} of {rows.length} lines. Open the
          file to read the rest.
        </div>
      ) : null}
    </div>
  );
}

window.DiffView = DiffView;
window.ST2_diffLines = ST2_diffLines;
window.ST2_parseUnifiedDiff = ST2_parseUnifiedDiff;
window.ST2_collapseContext = ST2_collapseContext;
window.ST2_pairRows = ST2_pairRows;
window.ST2_splitLines = ST2_splitLines;
window.ST2_DIFF_MAX_BYTES = ST2_DIFF_MAX_BYTES;
window.ST2_DIFF_MAX_CELLS = ST2_DIFF_MAX_CELLS;
window.ST2_DIFF_ROW_CAP = ST2_DIFF_ROW_CAP;
window.ST2_SIDE_BY_SIDE_MIN_W = ST2_SIDE_BY_SIDE_MIN_W;
