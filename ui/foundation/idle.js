// primer UI — idle-mode polling pause.
//
// Sets window.primerApi.idle = true after 5 minutes of no user input
// while the page is visible. useResource's pauseWhile() consults this
// flag; when true, scheduled polls are skipped (in-flight fetches
// continue to resolve normally).
//
// On the first user input after idle, the flag clears and every
// active resource refetches immediately via window.primerApi._refetchAll
// so the operator sees current state without manually reloading.
//
// Loaded after the other foundation modules in ui/index.html so
// _refetchAll exists by the time we wire the input listeners.

(function () {
  const ns = (window.primerApi = window.primerApi || {});
  ns.idle = false;

  const IDLE_AFTER_MS = 5 * 60 * 1000;   // 5 minutes
  const CHECK_INTERVAL_MS = 30 * 1000;   // re-evaluate every 30s

  let lastInput = Date.now();

  const onInput = () => {
    lastInput = Date.now();
    if (ns.idle) {
      ns.idle = false;
      // The idle flag flipping false alone doesn't re-arm the polls
      // (they're scheduled per-settle); kick every active entry now
      // so the UI catches up immediately.
      if (typeof ns._refetchAll === "function") ns._refetchAll();
    }
  };

  // Passive listeners; never preventDefault.
  ["mousemove", "keydown", "scroll", "touchstart", "pointerdown"].forEach((ev) => {
    document.addEventListener(ev, onInput, { passive: true });
  });

  setInterval(() => {
    const visible = document.visibilityState === "visible";
    ns.idle = visible && (Date.now() - lastInput > IDLE_AFTER_MS);
  }, CHECK_INTERVAL_MS);

  // Expose for foundation tests (in __tests__/foundation.test.html and
  // any future driver) — read-only inspection of internal state.
  ns._idle = {
    getLastInput: () => lastInput,
    forceCheck: () => {
      const visible = document.visibilityState === "visible";
      ns.idle = visible && (Date.now() - lastInput > IDLE_AFTER_MS);
      return ns.idle;
    },
    // Test seam: lets a test stub lastInput backwards/forwards.
    _setLastInput: (ts) => { lastInput = ts; },
  };
})();
