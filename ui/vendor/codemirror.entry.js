// Bundle entry for primer's vendored CodeMirror 6.
//
// Exposes ONE global (window.CM6) holding only the pieces the python
// toolset editor uses. Nothing else from the CodeMirror tree is reachable
// from the console, which keeps the vendored surface auditable: if a name
// is not on this object, it is not callable from ui/.
//
// Built to IIFE (not ESM) because ui/index.html loads plain <script> tags
// into one shared Babel scope -- same convention as g6.min.js and
// xterm.min.js.

import {
  EditorState, Compartment, StateEffect, RangeSetBuilder,
} from "@codemirror/state";
import {
  EditorView, keymap, lineNumbers, highlightActiveLine,
  highlightActiveLineGutter, drawSelection, rectangularSelection,
  crosshairCursor, placeholder, Decoration, ViewPlugin,
} from "@codemirror/view";
import {
  defaultKeymap, history, historyKeymap, indentWithTab,
} from "@codemirror/commands";
import {
  syntaxHighlighting, HighlightStyle, indentUnit, foldGutter, foldKeymap,
  bracketMatching, indentOnInput, StreamLanguage,
} from "@codemirror/language";
import { python, pythonLanguage } from "@codemirror/lang-python";
import {
  autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap,
  startCompletion,
} from "@codemirror/autocomplete";
import { linter, lintGutter, setDiagnostics, forceLinting } from "@codemirror/lint";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { tags } from "@lezer/highlight";

globalThis.CM6 = {
  // state
  EditorState, Compartment, StateEffect, RangeSetBuilder,
  // view
  EditorView, keymap, lineNumbers, highlightActiveLine,
  highlightActiveLineGutter, drawSelection, rectangularSelection,
  crosshairCursor, placeholder, Decoration, ViewPlugin,
  // commands
  defaultKeymap, history, historyKeymap, indentWithTab,
  // language
  syntaxHighlighting, HighlightStyle, indentUnit, foldGutter, foldKeymap,
  bracketMatching, indentOnInput, StreamLanguage,
  python, pythonLanguage,
  // completion
  autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap,
  startCompletion,
  // lint (server diagnostics are pushed with setDiagnostics)
  linter, lintGutter, setDiagnostics, forceLinting,
  // search
  searchKeymap, highlightSelectionMatches,
  // highlight tags, for the theme defined in ui/
  tags,
};
