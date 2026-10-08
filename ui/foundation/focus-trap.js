// primer UI - one focus trap for every dialog.
//
// useFocusTrap(ref, active, opts) keeps keyboard focus inside the dialog node while it is open and gives it back afterwards:
//   * on activation focus moves INTO the dialog (opts.initial(node) may name the element, else the first focusable, else the node);
//     focus already inside is left alone, which keeps an autoFocus'd input (the rename dialog) and ConfirmHost's delayed input focus;
//   * Tab and Shift+Tab cycle inside it, so a keyboard user cannot tab out into the (inert) page behind the scrim;
//   * on deactivation or unmount focus returns to the element that had it when the dialog opened, if that element still exists.
//
// The opener is captured DURING RENDER, on the first render in which the dialog is active, not in the effect: React runs an autoFocus
// during commit, before any effect, so by then focus is already inside the dialog and the real opener is gone.
//
// Extracted from shared.jsx's Modal (FC5a), which the console's own overlays (nv-overlays.jsx) and the phone's bottom sheet did not have
// (console review 2026-10-08, C-028). Plain JS, no JSX: it is loaded before the components that call it.

(function () {
  const ns = (window.primerApi = window.primerApi || {});

  const SELECTOR =
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

  // The focusable descendants of `node` that are actually shown.
  function focusablesOf(node) {
    return Array.prototype.slice
      .call(node.querySelectorAll(SELECTOR))
      .filter((el) => el.offsetParent !== null || el === document.activeElement);
  }

  // `deps` re-attaches the trap when the dialog's DOM node is replaced (Modal swaps its desktop and phone trees).
  function useFocusTrap(ref, active, opts, deps) {
    const o = opts || {};
    const openerRef = React.useRef(null);
    const wasActiveRef = React.useRef(false);
    if (active && !wasActiveRef.current && typeof document !== "undefined") {
      openerRef.current = document.activeElement;
    }
    wasActiveRef.current = !!active;

    React.useEffect(() => {
      if (!active) return undefined;
      const node = ref.current;
      if (!node) return undefined;
      if (!node.contains(document.activeElement)) {
        const first = (o.initial && o.initial(node)) || focusablesOf(node)[0] || node;
        if (first && first.focus) first.focus();
      }
      const onKeyDown = (e) => {
        if (e.key !== "Tab" || e.defaultPrevented) return;
        const items = focusablesOf(node);
        if (items.length === 0) { e.preventDefault(); if (node.focus) node.focus(); return; }
        const first = items[0];
        const last = items[items.length - 1];
        const current = document.activeElement;
        if (e.shiftKey) {
          if (current === first || current === node || !node.contains(current)) { e.preventDefault(); last.focus(); }
        } else if (current === last || !node.contains(current)) {
          e.preventDefault();
          first.focus();
        }
      };
      node.addEventListener("keydown", onKeyDown);
      return () => {
        node.removeEventListener("keydown", onKeyDown);
        const opener = openerRef.current;
        if (opener && typeof opener.focus === "function" && document.contains(opener)) opener.focus();
      };
    }, [active].concat(deps || []));
  }

  ns.useFocusTrap = useFocusTrap;
  ns.focusablesOf = focusablesOf;
})();
