// primer UI - one Escape stack for everything that closes on Escape.
//
// useEscape(handler, active) registers `handler` while the component is mounted and `active` is true. ONE keydown listener on the window
// answers Escape by calling the handler of the TOP-MOST registered thing only: a confirm dialog over an overlay closes the dialog and leaves
// the overlay (and the draft in it) alone, and the next Escape closes the overlay; two modals on top of each other close top first.
//
// Before this, the overlay panel and Modal each added their own window listener, so ONE Escape closed every layer that was listening: a
// confirm dialog over the graph builder closed the builder (unsaved draft lost) and left the dialog on screen (board task 01a12147).
//
// "Top-most" is the layer that became active last. The order is taken at RENDER time (a counter, set the first render in which the entry is
// active, cleared when it is not), not at effect time: React runs a child's effects before its parent's, so a modal that is open on the
// first render of the overlay it sits in would otherwise register UNDER it. A parent renders before its child, and a layer that opens later
// renders later, so both come out on top as they should. A handler that changes between renders keeps its place.
//
// An Escape that was already handled (`defaultPrevented`, as an input that closes its own suggestion list does) is not an Escape for the
// stack. The handler of the top entry decides what to do, and nothing below it hears the key, so a handler that refuses (a request in
// flight) keeps the whole stack as it is. Plain JS, no JSX: it is loaded before the components that call it.

(function () {
  const ns = (window.primerApi = window.primerApi || {});

  const entries = new Set();
  let counter = 0;

  function topmost() {
    let best = null;
    entries.forEach((entry) => {
      if (!best || entry.seq > best.seq) best = entry;
    });
    return best;
  }

  function onKeyDown(ev) {
    // An Escape that belongs to an IME composition (it cancels the candidate list) is not an Escape for a layer: Chrome says so with isComposing, Safari with keyCode 229 on the keydown that ends one.
    if (ev.key !== "Escape" || ev.defaultPrevented || ev.isComposing || ev.keyCode === 229) return;
    const top = topmost();
    if (top && typeof top.handler === "function") top.handler(ev);
  }

  function attach() {
    if (entries.size === 1) window.addEventListener("keydown", onKeyDown);
  }

  function detach() {
    if (entries.size === 0) window.removeEventListener("keydown", onKeyDown);
  }

  function useEscape(handler, active) {
    const on = active === undefined ? true : !!active;
    const live = React.useRef(null);
    if (live.current === null) live.current = { handler: handler, order: 0, seq: 0 };
    live.current.handler = handler;
    // `order` is taken while rendering; `seq`, the place on the stack, is only set when the layer is committed (a render React throws away moves nothing that is on the stack)
    if (on && live.current.order === 0) live.current.order = ++counter;
    if (!on) live.current.order = 0;

    React.useEffect(() => {
      if (!on) return undefined;
      const entry = live.current;
      entry.seq = entry.order;
      entries.add(entry);
      attach();
      return () => {
        entries.delete(entry);
        entry.seq = 0;
        detach();
      };
    }, [on]);
  }

  ns.useEscape = useEscape;
})();
