// matrix UI — useTweaks hook (module-level shared state).
//
// State lives at module scope so every consumer (App, TweaksPanel,
// Topbar, future pages) reads the same values and a setTweak from
// one renders all of them. The seed comes from whichever consumer
// calls useTweaks first with a non-null `defaults` argument, merged
// with DEFAULT_DEFAULTS below; later calls without arguments just
// read the established state.
//
// Persistence: we still postMessage('__edit_mode_set_keys', edits) to
// window.parent so a hosting editor frame (the mockup's original
// design target) can rewrite the EDITMODE block on disk. In the
// matrix console there is no parent frame, so the message is a no-op;
// in-page state is the actual store.

(function () {
  const DEFAULT_DEFAULTS = {
    theme: "dark",
    accent: "Matrix green",
    density: "default",
    demoState: "happy",
    subsystemOn: false,
    icState: "configured",
    // Added Milestone 2 — drives the topbar brand. Operator can
    // change it via the tweaks panel; persisted only client-side.
    instanceLabel: "matrix · localhost:8765",
  };

  const state = {
    values: { ...DEFAULT_DEFAULTS },
    listeners: new Set(),
    seeded: false,
  };

  function setTweak(keyOrEdits, val) {
    const edits = typeof keyOrEdits === "object" && keyOrEdits !== null
      ? keyOrEdits
      : { [keyOrEdits]: val };
    state.values = { ...state.values, ...edits };
    state.listeners.forEach((cb) => cb(state.values));
    try {
      window.parent.postMessage({ type: "__edit_mode_set_keys", edits }, "*");
    } catch (_e) { /* no-op outside an iframe host */ }
    try {
      window.dispatchEvent(new CustomEvent("tweakchange", { detail: edits }));
    } catch (_e) { /* defensive */ }
  }

  function useTweaks(defaults) {
    // Seed once: the first caller that supplies a `defaults` map wins.
    // Subsequent calls with arguments are ignored (they would clobber
    // any user changes made since seed). Calls without arguments just
    // subscribe.
    if (defaults && !state.seeded) {
      state.values = { ...DEFAULT_DEFAULTS, ...defaults };
      state.seeded = true;
    }
    const [snap, setSnap] = React.useState(state.values);
    React.useEffect(() => {
      const cb = (next) => setSnap(next);
      state.listeners.add(cb);
      // Sync immediately in case state moved between render and effect.
      setSnap(state.values);
      return () => { state.listeners.delete(cb); };
    }, []);
    return [snap, setTweak];
  }

  const ns = (window.matrixApi = window.matrixApi || {});
  ns.useTweaks = useTweaks;
  ns._tweaks = { state, setTweak };  // test seam
  // Bridge so legacy global references (app.jsx /* global useTweaks */ and
  // tweaks-panel.jsx's Object.assign(window, ...)) keep working.
  window.useTweaks = useTweaks;
})();
