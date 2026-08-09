/* global React */
// Workspace context: derived from the active session document, pinnable.
// Spec section 6 (revised 2026-08-09). There is NO manual selector; the
// chip explains, the palette pins.
(function () {
  let ws = null;
  let isPinned = false;
  const subs = new Set();
  const emit = () => subs.forEach((fn) => fn());

  window.S2_Ctx = {
    get() { return ws; },
    pinned() { return isPinned; },
    pin(id) { ws = id; isPinned = true; emit(); },
    unpin() { isPinned = false; emit(); },
    noteActiveDoc(kind, ref, wsId) {
      if (isPinned) return;
      if (kind === "session" && wsId && wsId !== ws) { ws = wsId; emit(); }
    },
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
    useCtx() {
      const [, setV] = React.useState(0);
      React.useEffect(() => window.S2_Ctx.subscribe(() => setV((x) => x + 1)), []);
      return { ws, pinned: isPinned };
    },
  };

  window.S2_Commands.register({
    id: "ctx:unpin", title: "Context: follow active tab", glyph: "▦",
    cat: "workspace", run: () => window.S2_Ctx.unpin(),
  });
  // Pin commands per workspace, refreshed from the shell's workspaces
  // poll. register() overwrites by id, so re-registering is idempotent.
  window.S2_registerCtxPins = (workspaces) => {
    for (const w of workspaces || []) {
      window.S2_Commands.register({
        id: "ctx:pin:" + w.id,
        title: "Context: pin to " + (w.name || w.id),
        glyph: "▦", cat: "workspace",
        run: () => window.S2_Ctx.pin(w.id),
      });
    }
  };
})();
