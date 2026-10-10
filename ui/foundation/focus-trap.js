// primer UI - one focus trap for every dialog.
//
// useFocusTrap(ref, active, opts) keeps keyboard focus inside the dialog node while it is open and gives it back afterwards:
//   * on activation focus moves INTO the dialog (opts.initial(node) may name the element, else the first focusable, else the node);
//     focus already inside is left alone, which keeps an autoFocus'd input (the rename dialog) and ConfirmHost's delayed input focus;
//   * Tab and Shift+Tab cycle inside it, so a keyboard user cannot tab out into the (inert) page behind the scrim. They wrap from the ends AND from any element that holds focus beyond them without
//     being a tab stop (a tabindex -1 heading a removal hands focus to): after the last tab stop, or inside it, a Tab wraps to the first; before the first, a Shift+Tab wraps to the last;
//   * on deactivation or unmount focus returns to the element that had it when the dialog opened, if that element still exists;
//   * a Tab (or Shift+Tab) while focus is NOT inside the top-most open dialog is answered by the document: a focused control that is disabled (a busy submit) or removed hands focus to <body>, where a key
//     never reaches the dialog node, and the next Tab used to go to the page behind the scrim (board task 01a124ad). Only the top-most dialog answers, so a dialog over an overlay takes the focus, not the one below.
//     A focus lost from the MIDDLE of the dialog continues from the place of the lost control, as the browser would have (review of #729, round 2); one with nothing to continue from restarts at the first stop (the
//     last on Shift+Tab).
//
// EVERY layer that holds focus above a dialog must be a trap itself. The document listener knows the open traps and nothing else: a layer that is not one (the add-step palette, the command palette were) is
// ignored, and a Tab from it goes to the first control of the dialog BEHIND its scrim, where the next Enter can close the overlay with a draft in it. tests/ui/test_focus_layers_are_traps.py keeps a
// role="dialog" from appearing without the hook.
//
// What counts as a stop is what the browser tabs to: not tabindex -1 (any negative), not disabled (the attribute, or a <fieldset disabled> above it), not inert, not hidden (display, content-visibility or
// visibility), a tabindex that is a number for an element that is not a control, a radio group once (the checked radio, else the first), the first <summary> of a <details> only, and contenteditable, iframe and
// audio/video with controls as well as links, buttons and fields. tests/ui_e2e/test_focus_stops_match_the_browser_journey.py compares the list with Chromium's own Tab order. NOT covered: keys pressed inside
// an iframe never reach the parent document, so an iframe that is the last stop is a place Tab can leave from; shadow DOM.
//
// The opener is captured DURING RENDER, on the first render in which the dialog is active, not in the effect: React runs an autoFocus
// during commit, before any effect, so by then focus is already inside the dialog and the real opener is gone.
//
// Extracted from shared.jsx's Modal (FC5a), which the console's own overlays (nv-overlays.jsx) and the phone's bottom sheet did not have
// (console review 2026-10-08, C-028). Plain JS, no JSX: it is loaded before the components that call it.

(function () {
  const ns = (window.primerApi = window.primerApi || {});

  // every clause says "not tabindex -1" (the browser skips any negative tabindex, a button or an input included)
  const STOP = ':not([tabindex^="-"])';
  // what the browser tabs to by what it IS. :disabled, not [disabled]: a control under a <fieldset disabled> is disabled too. A <summary> is a stop only as the first summary of its <details>.
  const NATIVE = [
    "a[href]" + STOP,
    "button:not(:disabled)" + STOP,
    "textarea:not(:disabled)" + STOP,
    "input:not(:disabled)" + STOP,
    "select:not(:disabled)" + STOP,
    "details > summary:first-of-type" + STOP,
    "iframe" + STOP,
    "audio[controls]" + STOP,
    "video[controls]" + STOP,
    '[contenteditable]:not([contenteditable="false"])' + STOP,
  ].join(", ");
  // anything else is a stop only with a tabindex
  const SELECTOR = NATIVE + ", [tabindex]" + STOP;

  // Shown: display:none and visibility:hidden (offsetParent alone saw the first and not the second, and none for a fixed-position control). With no checkVisibility (Safari before 17.4) a control is shown
  // when it has a box and its visibility is "visible" (hidden and collapse both hide it); getClientRects is empty for display:none, and a fixed-position control has one though it has no offsetParent.
  function isShown(el) {
    if (el === document.activeElement) return true;
    if (typeof el.checkVisibility === "function") return el.checkVisibility({ visibilityProperty: true, checkVisibilityCSS: true });
    return el.getClientRects().length > 0 && getComputedStyle(el).visibility === "visible";
  }

  // A stop the browser would tab to. `tabindex=""` (or "x") is no tabindex: it makes a div no stop, and a button keeps its own.
  function isStop(el) {
    if (el.closest && el.closest("[inert]")) return false;
    if (!el.matches(NATIVE) && !Number.isFinite(parseInt(el.getAttribute("tabindex"), 10))) return false;
    return isShown(el);
  }

  function isNamedRadio(el) {
    return el.type === "radio" && !!el.getAttribute("name");
  }

  // The browser stops once in a radio group: on the checked radio, else on the first. Which radio is `current` is judged by the stop of its group.
  function radioStops(items) {
    const groups = new Map();                  // form (or null) -> name -> radios, in document order
    items.forEach((el) => {
      if (!isNamedRadio(el)) return;
      const byName = groups.get(el.form || null) || new Map();
      const name = el.getAttribute("name");
      byName.set(name, (byName.get(name) || []).concat(el));
      groups.set(el.form || null, byName);
    });
    const keep = new Set();
    groups.forEach((byName) => byName.forEach((list) => keep.add(list.find((r) => r.checked) || list[0])));
    return keep;
  }

  // The focusable descendants of `node` that are actually shown, one per radio group.
  function focusablesOf(node) {
    const shown = Array.prototype.slice.call(node.querySelectorAll(SELECTOR)).filter(isStop);
    const keep = radioStops(shown);
    return shown.filter((el) => !isNamedRadio(el) || keep.has(el));
  }

  // A radio that is not its group's stop stands for the stop of its group when focus is compared with the ends.
  function stopOf(current, items) {
    if (!isNamedRadio(current)) return current;
    return items.find((el) => isNamedRadio(el) && el.getAttribute("name") === current.getAttribute("name") && (el.form || null) === (current.form || null)) || current;
  }

  // Wrap a Tab that would leave `node`. Handles the key itself (preventDefault) when it does; returns nothing.
  function wrapTab(node, e) {
    const items = focusablesOf(node);
    if (items.length === 0) { e.preventDefault(); if (node.focus) node.focus(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    const current = stopOf(document.activeElement, items);
    // Document order decides whether focus is beyond an end: an element that is no tab stop is not in `items`, so comparing with `first` and `last` alone let a Tab from after the last (or a Shift+Tab
    // from before the first) fall through to the browser, which walked on out of the dialog (board task 01a122c8-33c6).
    const beforeFirst = !!(first.compareDocumentPosition(current) & Node.DOCUMENT_POSITION_PRECEDING);
    const afterLast = !!(last.compareDocumentPosition(current) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (e.shiftKey) {
      if (current === first || current === node || !node.contains(current) || beforeFirst) { e.preventDefault(); last.focus(); }
    } else if (current === last || !node.contains(current) || afterLast) {
      e.preventDefault();
      first.focus();
    }
  }

  // The last element focused inside `node` and how many stops precede it, so that a focus lost to <body> (the control was disabled or removed) can continue from its place.
  function remember(trap, target) {
    const items = focusablesOf(trap.node);
    const at = stopOf(target, items);
    const before = items.filter((it) => it !== at && !!(at.compareDocumentPosition(it) & Node.DOCUMENT_POSITION_PRECEDING)).length;
    trap.lost = { el: target, before };
  }

  // Where the browser would take the Tab (Shift+Tab) from the place of the lost control, inside `trap.node`; null when there is nothing to continue from.
  function continuation(trap, shift) {
    const memory = trap.lost;
    if (!memory) return null;
    const items = focusablesOf(trap.node);
    if (items.length === 0) return null;
    const there = trap.node.contains(memory.el) && document.contains(memory.el);     // disabled or blurred in place, not removed
    const n = there ? items.filter((it) => it !== memory.el && !!(memory.el.compareDocumentPosition(it) & Node.DOCUMENT_POSITION_PRECEDING)).length : memory.before;
    const next = shift ? n - 1 : (items[n] === memory.el ? n + 1 : n);
    return items[((next % items.length) + items.length) % items.length];
  }

  // The open dialogs, each with the order in which it became active (set during render, as the opener is, so that re-attaching a dialog does not move it above one opened later).
  const traps = [];
  let nextOrder = 0;
  function onDocumentKeyDown(e) {
    if (e.key !== "Tab" || e.defaultPrevented) return;
    let top = null;
    traps.forEach((t) => { if (!top || t.order > top.order) top = t; });
    if (!top || top.node.contains(document.activeElement)) return;       // inside it, the dialog's own listener answers
    if (document.activeElement === document.body || !document.activeElement) {
      const target = continuation(top, !!e.shiftKey);
      if (target) { e.preventDefault(); target.focus(); return; }
    }
    wrapTab(top.node, e);
  }
  function addTrap(trap) {
    traps.push(trap);
    if (traps.length === 1) document.addEventListener("keydown", onDocumentKeyDown);
  }
  function removeTrap(trap) {
    const i = traps.indexOf(trap);
    if (i >= 0) traps.splice(i, 1);
    if (traps.length === 0) document.removeEventListener("keydown", onDocumentKeyDown);
  }

  // `deps` re-attaches the trap when the dialog's DOM node is replaced (Modal swaps its desktop and phone trees).
  function useFocusTrap(ref, active, opts, deps) {
    const o = opts || {};
    const openerRef = React.useRef(null);
    const wasActiveRef = React.useRef(false);
    const orderRef = React.useRef(0);
    if (active && !wasActiveRef.current && typeof document !== "undefined") {
      openerRef.current = document.activeElement;
      orderRef.current = ++nextOrder;
    }
    wasActiveRef.current = !!active;

    React.useEffect(() => {
      if (!active) return undefined;
      const node = ref.current;
      if (!node) return undefined;
      const trap = { node, order: orderRef.current, lost: null };
      // registered BEFORE the first focus move, so the focus the trap gives the dialog is remembered too
      const onFocusIn = (e) => { if (e.target && e.target !== node) remember(trap, e.target); };
      node.addEventListener("focusin", onFocusIn);
      if (!node.contains(document.activeElement)) {
        const first = (o.initial && o.initial(node)) || focusablesOf(node)[0] || node;
        if (first && first.focus) first.focus();
      } else if (document.activeElement !== node) {
        remember(trap, document.activeElement);                          // an autoFocus'd input already holds it
      }
      const onKeyDown = (e) => {
        if (e.key !== "Tab" || e.defaultPrevented) return;
        wrapTab(node, e);
      };
      node.addEventListener("keydown", onKeyDown);
      addTrap(trap);
      return () => {
        removeTrap(trap);
        node.removeEventListener("keydown", onKeyDown);
        node.removeEventListener("focusin", onFocusIn);
        const opener = openerRef.current;
        if (opener && typeof opener.focus === "function" && document.contains(opener)) opener.focus();
      };
    }, [active].concat(deps || []));
  }

  ns.useFocusTrap = useFocusTrap;
  ns.focusablesOf = focusablesOf;
})();
