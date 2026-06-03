// primer UI — useTweaks hook (module-level shared state).
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
// primer console there is no parent frame, so the message is a no-op;
// in-page state is the actual store.

(function () {
  const DEFAULT_DEFAULTS = {
    theme: "dark",
    accent: "Primer green",
    density: "default",
    demoState: "happy",
    subsystemOn: false,
    icState: "configured",
    // Added Milestone 2 — drives the topbar brand. Operator can
    // change it via the tweaks panel; persisted only client-side.
    instanceLabel: "primer · localhost:8765",
  };

  // Subset of keys whose user choice we persist across reloads via
  // localStorage. Currently just `theme` (operator-toggleable from
  // the topbar). Adding more keys is a one-line addition here; we
  // deliberately don't persist demo-state knobs like `demoState` /
  // `subsystemOn` since those are mockup leftovers, not real
  // operator preferences.
  const PERSISTED_KEYS = new Set(["theme"]);
  const STORAGE_KEY = "primer.tweaks";

  function _readPersisted() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return {};
      const out = {};
      for (const k of PERSISTED_KEYS) {
        if (k in parsed) out[k] = parsed[k];
      }
      return out;
    } catch (_e) {
      return {};
    }
  }

  function _writePersisted(values) {
    try {
      const subset = {};
      for (const k of PERSISTED_KEYS) {
        if (k in values) subset[k] = values[k];
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(subset));
    } catch (_e) { /* private mode, quota, etc. — non-fatal */ }
  }

  const _persistedInitial = _readPersisted();
  const state = {
    values: { ...DEFAULT_DEFAULTS, ..._persistedInitial },
    listeners: new Set(),
    seeded: false,
  };
  // Apply the persisted theme to the document root SYNCHRONOUSLY at
  // script load — before React mounts and the app.jsx effect runs —
  // so a reloaded page in light mode doesn't flash dark for a few
  // hundred ms.
  try {
    if (_persistedInitial.theme) {
      document.documentElement.setAttribute("data-theme", _persistedInitial.theme);
    }
  } catch (_e) { /* defensive: no document yet, etc. */ }

  function setTweak(keyOrEdits, val) {
    const edits = typeof keyOrEdits === "object" && keyOrEdits !== null
      ? keyOrEdits
      : { [keyOrEdits]: val };
    state.values = { ...state.values, ...edits };
    state.listeners.forEach((cb) => cb(state.values));
    // Persist the persistent-key subset so refreshes preserve the
    // operator's choice (currently: theme).
    const touchedPersistent = Object.keys(edits).some((k) => PERSISTED_KEYS.has(k));
    if (touchedPersistent) _writePersisted(state.values);
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
      // Persisted values win over both DEFAULT_DEFAULTS and any
      // first-caller `defaults` so the operator's saved theme isn't
      // clobbered on the first render.
      state.values = { ...DEFAULT_DEFAULTS, ...defaults, ..._readPersisted() };
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

  const ns = (window.primerApi = window.primerApi || {});
  ns.useTweaks = useTweaks;
  ns._tweaks = { state, setTweak };  // test seam
  // Bridge so legacy global references (app.jsx /* global useTweaks */ and
  // tweaks-panel.jsx's Object.assign(window, ...)) keep working.
  window.useTweaks = useTweaks;
})();
