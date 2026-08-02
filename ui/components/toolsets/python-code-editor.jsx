/* global React */

// CodeMirror 6 wrapper for the python toolset editor, plus the two things
// that make it primer-aware rather than a generic Python box: completions
// over primer's own surface, and the commented scaffolds the "Add function"
// menu inserts.
//
// The vendored bundle (ui/vendor/codemirror.min.js) exposes one global,
// window.CM6, holding only the names used here. If it fails to load, the
// editor degrades to a plain textarea rather than rendering nothing -- an
// operator who cannot load 447KB of editor should still be able to fix a
// broken tool.
//
// Babel-standalone shares one global scope across script tags, so every
// top-level binding here is prefixed PY_.

// ---------------------------------------------------------------------------
// Scaffolds -- what "Add function" inserts.
//
// The comments are the point. They are the only place the contract is stated
// at the moment the operator is writing the thing it constrains: which parts
// of the docstring are load-bearing, which parameter name is magic, and what
// is configurable. They are ordinary Python comments, so deleting them is the
// obvious next step once the shape is learned.
// ---------------------------------------------------------------------------

var PY_SCAFFOLD_TOOL = [
  "",
  "# --------------------------------------------------------------------",
  "# @primer_tool marks a function as a callable tool. Options:",
  "#",
  "#   timeout_seconds=N   per-tool wall clock. Falls back to the toolset's",
  "#                       default; 300 is the hard ceiling.",
  "#",
  "# The docstring is the contract the model sees, not decoration:",
  "#   first line    what the tool does",
  "#   'Use when'    when the model should reach for it (required)",
  "#   Args:         one entry per parameter (required -- a missing entry",
  "#                 fails registration and names the parameter)",
  "#   Examples:     optional JSON objects, checked against the schema",
  "#",
  "# Every parameter needs a type annotation and becomes an argument.",
  "# A parameter named `ctx` is the exception: primer injects it and keeps",
  "# it out of the schema.",
  "# --------------------------------------------------------------------",
  "@primer_tool()",
  "def my_tool(text: str) -> str:",
  '    """One line describing what this does.',
  "",
  "    Use when the model needs to do the thing this does.",
  "",
  "    Args:",
  "        text: What this argument is for.",
  '    """',
  "    return text",
  "",
].join("\n");

var PY_SCAFFOLD_YIELDING = [
  "",
  "# --------------------------------------------------------------------",
  "# A YIELDING tool. It parks the run instead of returning a value, and",
  "# resumes when the thing it waited for happens.",
  "#",
  "# Two functions are required:",
  "#   1. the tool itself, which calls a yield helper and takes `ctx`",
  "#   2. a @resumes(<tool>) companion, which turns the eventual payload",
  "#      into the tool's result",
  "#",
  "# Yield helpers: ask_user(question), sleep_for(seconds),",
  "#                watch_files(paths)",
  "#",
  "# The companion is NOT itself a tool -- it has no @primer_tool, so the",
  "# model never sees it.",
  "# --------------------------------------------------------------------",
  "@primer_tool()",
  "async def ask_the_operator(question: str, ctx) -> str:",
  '    """Ask the operator a question and wait for the answer.',
  "",
  "    Use when a decision needs a human.",
  "",
  "    Args:",
  "        question: What to ask.",
  '    """',
  "    return ask_user(question)",
  "",
  "",
  "@resumes(ask_the_operator)",
  "def _ask_the_operator_resume(payload: dict, meta: dict) -> str:",
  '    """Return the operator\'s answer.',
  "",
  "    Use when resuming the question.",
  "",
  "    Args:",
  "        payload: The event payload.",
  "        meta: The resume metadata.",
  '    """',
  "    return payload[\"response\"]",
  "",
].join("\n");

var PY_SCAFFOLDS = [
  {
    id: "tool",
    label: "Tool",
    hint: "A function the model can call and get a value back from.",
    source: PY_SCAFFOLD_TOOL,
  },
  {
    id: "yielding",
    label: "Yielding tool",
    hint: "Parks the run until a human answers, a timer fires, or files change.",
    source: PY_SCAFFOLD_YIELDING,
  },
];

// ---------------------------------------------------------------------------
// Completions.
//
// Deliberately NOT general Python. An operator writing a 20-line tool does
// not need `str.` members; they need the six names primer injects, which
// appear in no Python documentation anywhere. Each carries `info` because
// the completion list is the only place this surface is discoverable.
// ---------------------------------------------------------------------------

var PY_API_COMPLETIONS = [
  {
    label: "@primer_tool()",
    type: "keyword",
    info: "Marks the function below as a callable tool. Takes an optional "
      + "timeout_seconds=N (hard ceiling 300).",
  },
  {
    label: "@resumes",
    type: "keyword",
    apply: "@resumes()",
    info: "@resumes(some_tool) marks this function as that tool's resume "
      + "companion: it converts the event payload into the tool's result. "
      + "It is not itself a tool.",
  },
  {
    label: "ask_user",
    type: "function",
    apply: "ask_user()",
    info: "ask_user(question, meta=None) -- park the run and ask the "
      + "operator. Requires a @resumes companion. The tool must take ctx.",
  },
  {
    label: "sleep_for",
    type: "function",
    apply: "sleep_for()",
    info: "sleep_for(seconds, meta=None) -- park the run for a duration "
      + "without holding a worker. Requires a @resumes companion.",
  },
  {
    label: "watch_files",
    type: "function",
    apply: "watch_files()",
    info: "watch_files(paths, meta=None) -- park until one of the paths "
      + "changes in the workspace. Requires a @resumes companion.",
  },
  {
    label: "ctx",
    type: "variable",
    info: "Injected by primer when a parameter is named ctx. Not part of "
      + "the argument schema. Carries ctx.inform(...) for progress and the "
      + "identifiers a yield needs.",
  },
];

// Docstring section headers, offered inside a docstring.
var PY_DOC_COMPLETIONS = [
  {
    label: "Use when",
    type: "text",
    apply: "Use when ",
    info: "Required. Tells the model when to reach for this tool. Without "
      + "it registration fails.",
  },
  {
    label: "Args:",
    type: "text",
    info: "Required section. One indented `name: description` per "
      + "parameter. A parameter with no entry fails registration.",
  },
  {
    label: "Examples:",
    type: "text",
    info: "Optional. Indented JSON objects of arguments; each is validated "
      + "against the generated schema, so a wrong example fails "
      + "registration rather than shipping a lie to the model.",
  },
];

// Roughly "is the cursor inside a triple-quoted string". Counting delimiters
// before the cursor is not a parser, but the consequence of being wrong is
// only which of two small lists is offered.
function PY_inDocstring(textBefore) {
  var m = textBefore.match(/"""/g);
  return !!m && m.length % 2 === 1;
}

function PY_completionSource(context) {
  var word = context.matchBefore(/[\w@]+/);
  if (!word && !context.explicit) return null;
  var from = word ? word.from : context.pos;
  var before = context.state.doc.sliceString(0, context.pos);
  var options = PY_inDocstring(before)
    ? PY_DOC_COMPLETIONS.concat(PY_API_COMPLETIONS)
    : PY_API_COMPLETIONS;
  return { from: from, options: options, validFor: /^[\w@]*$/ };
}

// ---------------------------------------------------------------------------
// Theme -- primer's CSS variables rather than a bundled colour scheme, so the
// editor follows the console's light/dark toggle for free.
// ---------------------------------------------------------------------------

function PY_buildTheme(C) {
  return C.EditorView.theme({
    "&": {
      fontSize: "var(--fs-12)",
      backgroundColor: "var(--bg)",
      color: "var(--text)",
      border: "1px solid var(--border)",
      borderRadius: "8px",
      height: "100%",
    },
    "&.cm-focused": { outline: "none", borderColor: "var(--accent-border)" },
    ".cm-content": {
      fontFamily: '"IBM Plex Mono", monospace',
      lineHeight: "1.6",
      caretColor: "var(--accent)",
    },
    ".cm-gutters": {
      backgroundColor: "var(--bg-1)",
      color: "var(--text-3)",
      border: "none",
      borderRight: "1px solid var(--border)",
    },
    ".cm-activeLine": { backgroundColor: "var(--bg-hover)" },
    ".cm-activeLineGutter": {
      backgroundColor: "var(--bg-hover)",
      color: "var(--text-2)",
    },
    ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
      backgroundColor: "var(--accent-dim)",
    },
    ".cm-tooltip": {
      backgroundColor: "var(--bg-elev)",
      border: "1px solid var(--border-strong)",
      borderRadius: "7px",
      color: "var(--text)",
    },
    ".cm-tooltip-autocomplete ul li[aria-selected]": {
      backgroundColor: "var(--accent-dim)",
      color: "var(--text)",
    },
    ".cm-completionInfo": {
      backgroundColor: "var(--bg-elev)",
      border: "1px solid var(--border-strong)",
      borderRadius: "7px",
      padding: "7px 9px",
      maxWidth: "320px",
      lineHeight: "1.5",
    },
    // The console flips light/dark by swapping data-theme on <html>, and
    // every colour above is a CSS variable, so the editor re-paints with the
    // rest of the page and needs no reconfiguration. `dark` is passed only so
    // CodeMirror's own defaults for the few surfaces not themed here start on
    // the right side; a stale value after a toggle costs nothing visible.
  }, { dark: PY_isDarkTheme() });
}

function PY_isDarkTheme() {
  if (typeof document === "undefined") return true;
  var attr = document.documentElement.getAttribute("data-theme");
  if (attr) return attr !== "light";
  return !window.matchMedia
    || !window.matchMedia("(prefers-color-scheme: light)").matches;
}

function PY_buildHighlight(C) {
  var t = C.tags;
  return C.HighlightStyle.define([
    { tag: t.keyword, color: "var(--violet)" },
    { tag: t.comment, color: "var(--text-3)", fontStyle: "italic" },
    { tag: [t.string, t.special(t.string)], color: "var(--accent)" },
    { tag: t.number, color: "var(--amber)" },
    { tag: t.definition(t.variableName), color: "var(--blue)" },
    { tag: t.function(t.variableName), color: "var(--blue)" },
    { tag: t.propertyName, color: "var(--text)" },
    { tag: t.typeName, color: "var(--teal)" },
    { tag: t.operator, color: "var(--text-2)" },
    { tag: t.meta, color: "var(--amber)" },
  ]);
}

// ---------------------------------------------------------------------------
// The editor component.
// ---------------------------------------------------------------------------

function PY_CodeEditor({ value, onChange, diagnostics, viewRef, minHeight }) {
  var hostRef = React.useRef(null);
  var localViewRef = React.useRef(null);
  // Kept in a ref so the CM6 update listener (created once, at mount) always
  // reaches the current callback without tearing the editor down per render.
  var onChangeRef = React.useRef(onChange);
  onChangeRef.current = onChange;

  var available = typeof window !== "undefined" && !!window.CM6;

  React.useEffect(function () {
    if (!available || !hostRef.current) return undefined;
    var C = window.CM6;

    var view = new C.EditorView({
      parent: hostRef.current,
      state: C.EditorState.create({
        doc: value || "",
        extensions: [
          C.lineNumbers(),
          C.foldGutter(),
          C.lintGutter(),
          C.history(),
          C.drawSelection(),
          C.indentOnInput(),
          C.bracketMatching(),
          C.closeBrackets(),
          C.highlightActiveLine(),
          C.highlightActiveLineGutter(),
          C.highlightSelectionMatches(),
          C.indentUnit.of("    "),
          C.python(),
          C.syntaxHighlighting(PY_buildHighlight(C)),
          C.autocompletion({ override: [PY_completionSource] }),
          PY_buildTheme(C),
          C.EditorView.lineWrapping,
          C.keymap.of([
            // indentWithTab first: in a code editor Tab should indent, and
            // the operator can still reach the next control with Escape+Tab.
            C.indentWithTab,
            ...C.closeBracketsKeymap,
            ...C.completionKeymap,
            ...C.searchKeymap,
            ...C.historyKeymap,
            ...C.defaultKeymap,
          ]),
          C.EditorView.updateListener.of(function (u) {
            if (u.docChanged && onChangeRef.current) {
              onChangeRef.current(u.state.doc.toString());
            }
          }),
        ],
      }),
    });

    localViewRef.current = view;
    if (viewRef) viewRef.current = view;
    return function () {
      view.destroy();
      localViewRef.current = null;
      if (viewRef) viewRef.current = null;
    };
    // Mount once. Value/diagnostics are pushed by the effects below; putting
    // them here would rebuild the editor on every keystroke and lose the
    // cursor.
  }, [available]);

  // External value changes (save reset, scaffold insert from elsewhere).
  // Guarded on inequality so typing does not dispatch its own text back.
  React.useEffect(function () {
    var view = localViewRef.current;
    if (!view) return;
    var current = view.state.doc.toString();
    if ((value || "") === current) return;
    view.dispatch({
      changes: { from: 0, to: current.length, insert: value || "" },
    });
  }, [value]);

  React.useEffect(function () {
    var view = localViewRef.current;
    if (!view || !window.CM6) return;
    view.dispatch(window.CM6.setDiagnostics(view.state, diagnostics || []));
  }, [diagnostics]);

  if (!available) {
    // Bundle missing. A textarea still lets an operator fix a broken tool.
    return (
      <textarea
        data-testid="python-source"
        data-editor="fallback"
        className="input mono"
        value={value || ""}
        spellCheck={false}
        onChange={function (e) { if (onChange) onChange(e.target.value); }}
        style={{
          width: "100%", minHeight: minHeight || 460, resize: "vertical",
          fontSize: "var(--fs-12)", lineHeight: 1.6, whiteSpace: "pre",
        }}
      />
    );
  }

  return (
    <div
      data-testid="python-source"
      data-editor="codemirror"
      ref={hostRef}
      style={{ minHeight: minHeight || 460, overflow: "hidden" }}
    />
  );
}

// Move the cursor to a 1-based line and centre it. Used by the outline.
function PY_revealLine(view, lineno) {
  if (!view || !lineno) return;
  var doc = view.state.doc;
  var n = Math.max(1, Math.min(lineno, doc.lines));
  var line = doc.line(n);
  view.dispatch({
    selection: { anchor: line.from },
    effects: window.CM6.EditorView.scrollIntoView(line.from, { y: "center" }),
  });
  view.focus();
}

// Insert text at the end of the document, and put the cursor in it.
function PY_appendSource(view, text) {
  if (!view) return;
  var end = view.state.doc.length;
  view.dispatch({
    changes: { from: end, insert: text },
    selection: { anchor: end + text.length },
  });
  view.focus();
}

window.PY_CodeEditor = PY_CodeEditor;
window.PY_SCAFFOLDS = PY_SCAFFOLDS;
window.PY_API_COMPLETIONS = PY_API_COMPLETIONS;
window.PY_DOC_COMPLETIONS = PY_DOC_COMPLETIONS;
window.PY_completionSource = PY_completionSource;
window.PY_revealLine = PY_revealLine;
window.PY_appendSource = PY_appendSource;
