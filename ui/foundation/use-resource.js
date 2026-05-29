// primer UI — useResource hook (polling, dedupe, abort, stale-while-error).
// Loaded via <script type="text/babel"> in ui/index.html. Depends on
// React (global) and may use window.primerApi.apiFetch indirectly via
// the caller's `fetcher` argument.

(function () {
  const { useState, useEffect, useRef, useCallback } = window.React;

  const MAX_ERRORS = 3;
  const cache = new Map(); // effectiveKey -> entry

  function getOrCreate(key) {
    let entry = cache.get(key);
    if (!entry) {
      entry = {
        data: undefined,
        error: null,
        loading: false,
        errorCount: 0,
        subscribers: new Set(),
        abortCtrl: null,
        timer: null,
        fetcher: null,
        pollMs: 0,
        pauseWhile: null,
      };
      cache.set(key, entry);
    }
    return entry;
  }

  function snapshotOf(entry) {
    return { data: entry.data, error: entry.error, loading: entry.loading };
  }

  // Reference + structural equality. Used to skip re-renders when a
  // poll returns identical data. The serialise-then-compare path is
  // good-enough for the JSON shapes the API returns; we deliberately
  // stop at one level of stringify so a very deep response still
  // converges in constant memory.
  function _eq(a, b) {
    if (a === b) return true;
    if (a == null || b == null) return false;
    if (typeof a !== "object" || typeof b !== "object") return false;
    try {
      return JSON.stringify(a) === JSON.stringify(b);
    } catch {
      return false;
    }
  }

  function emit(entry, force) {
    const snap = snapshotOf(entry);
    // Skip the broadcast if nothing observable changed since the last
    // emit. Without this every 5s poll fires setSnap with a new object
    // reference even when data is identical, forcing every consuming
    // component to re-render. The page-level App calls ~10 useResource
    // hooks on a 5s cadence, so the savings cascade.
    if (
      !force
      && entry._lastSnap
      && entry._lastSnap.loading === snap.loading
      && entry._lastSnap.error === snap.error
      && _eq(entry._lastSnap.data, snap.data)
    ) {
      return;
    }
    entry._lastSnap = snap;
    for (const cb of entry.subscribers) cb(snap);
  }

  function clearTimer(entry) {
    if (entry.timer != null) {
      clearTimeout(entry.timer);
      entry.timer = null;
    }
  }

  function abortInflight(entry) {
    if (entry.abortCtrl) {
      try {
        entry.abortCtrl.abort();
      } catch (_e) {
        // no-op
      }
      entry.abortCtrl = null;
    }
  }

  function schedule(entry, key) {
    clearTimer(entry);
    if (!entry.fetcher) return;
    if (!(entry.pollMs > 0)) return;
    if (entry.errorCount >= MAX_ERRORS) return;
    if (entry.pauseWhile && entry.pauseWhile()) return;
    entry.timer = setTimeout(() => {
      entry.timer = null;
      runFetch(entry, key);
    }, entry.pollMs);
  }

  async function runFetch(entry, key) {
    abortInflight(entry);
    const ctrl = new AbortController();
    entry.abortCtrl = ctrl;
    // loading=true is only meaningful for the FIRST fetch ("show
    // skeleton, no data yet"). Subsequent polls retain stale data —
    // flipping loading during a background poll just causes the
    // consuming component to flicker between "show data" and "show
    // loading" twice per cycle.
    if (entry.data === undefined) {
      entry.loading = true;
      emit(entry);
    }
    try {
      const data = await entry.fetcher(ctrl.signal);
      if (entry.abortCtrl !== ctrl) return; // superseded
      entry.data = data;
      entry.error = null;
      entry.loading = false;
      entry.errorCount = 0;
      entry.abortCtrl = null;
      emit(entry);
      schedule(entry, key);
    } catch (e) {
      if (e && (e.name === "AbortError" || e.code === 20)) return;
      if (entry.abortCtrl !== ctrl) return;
      entry.error = e;
      entry.loading = false;
      entry.errorCount += 1;
      entry.abortCtrl = null;
      // data retained (stale-while-error)
      emit(entry);
      schedule(entry, key);
    }
  }

  // Single page-wide visibilitychange listener; cancel/clear on hide,
  // immediate refetch + resume on show.
  let visibilityBound = false;
  function ensureVisibility() {
    if (visibilityBound) return;
    visibilityBound = true;
    document.addEventListener("visibilitychange", () => {
      const hidden = document.hidden;
      for (const [key, entry] of cache) {
        if (hidden) {
          abortInflight(entry);
          clearTimer(entry);
        } else if (entry.subscribers.size > 0 && entry.fetcher) {
          entry.errorCount = 0;
          runFetch(entry, key);
        }
      }
    });
  }

  function composeKey(cacheKey, deps) {
    if (!deps || deps.length === 0) return cacheKey;
    return cacheKey + "::" + JSON.stringify(deps);
  }

  function useResource(cacheKey, fetcher, opts = {}) {
    const { pollMs = 0, pauseWhile, deps } = opts;
    const effectiveKey = composeKey(cacheKey, deps);

    const [snap, setSnap] = useState(() => {
      const entry = cache.get(effectiveKey);
      return entry
        ? snapshotOf(entry)
        : { data: undefined, error: null, loading: true };
    });

    const fetcherRef = useRef(fetcher);
    fetcherRef.current = fetcher;
    const pauseWhileRef = useRef(pauseWhile);
    pauseWhileRef.current = pauseWhile;

    useEffect(() => {
      ensureVisibility();
      const entry = getOrCreate(effectiveKey);
      // Latest-wins: every render refreshes the entry's behaviour hooks
      // so a different pollMs/pauseWhile/fetcher passed by a later caller
      // takes effect on the next settle.
      entry.fetcher = (signal) => fetcherRef.current(signal);
      entry.pollMs = pollMs;
      entry.pauseWhile = () => {
        // Two stacked pause sources: the caller's pauseWhile and the
        // global idle flag set by ui/foundation/idle.js. Either pausing
        // means we skip the next scheduled poll. When the idle flag
        // clears, idle.js calls refetchAll() to resume every active
        // entry immediately.
        const fn = pauseWhileRef.current;
        if (typeof fn === "function" && fn()) return true;
        if (window.primerApi && window.primerApi.idle === true) return true;
        return false;
      };

      const isFirst = entry.subscribers.size === 0;
      const cb = (s) => setSnap(s);
      entry.subscribers.add(cb);

      if (isFirst) {
        runFetch(entry, effectiveKey);
      } else {
        // Sync the new subscriber to the entry's current state.
        setSnap(snapshotOf(entry));
      }

      return () => {
        entry.subscribers.delete(cb);
        if (entry.subscribers.size === 0) {
          abortInflight(entry);
          clearTimer(entry);
          cache.delete(effectiveKey);
        }
      };
    }, [effectiveKey, pollMs]);

    const refetch = useCallback(() => {
      const entry = cache.get(effectiveKey);
      if (!entry) return;
      entry.errorCount = 0;
      runFetch(entry, effectiveKey);
    }, [effectiveKey]);

    return { data: snap.data, error: snap.error, loading: snap.loading, refetch };
  }

  // Internal helpers exposed for useMutation's optimistic/invalidates
  // contract. Not part of the public component-facing API.
  function findKeys(target) {
    const keys = [];
    const prefix = target + "::";
    for (const k of cache.keys()) {
      if (k === target || k.startsWith(prefix)) keys.push(k);
    }
    return keys;
  }

  function peekData(key) {
    const entry = cache.get(key);
    return entry ? entry.data : undefined;
  }

  function replaceData(key, newData) {
    const entry = cache.get(key);
    if (!entry) return;
    entry.data = newData;
    entry.error = null;
    emit(entry);
  }

  function refetchKey(key) {
    const entry = cache.get(key);
    if (!entry || !entry.fetcher) return;
    entry.errorCount = 0;
    runFetch(entry, key);
  }

  // Re-fire every active entry (those with at least one subscriber and
  // a registered fetcher). Used by the idle module on wake-up to
  // resync the UI after a quiet period.
  function refetchAll() {
    for (const [key, entry] of cache) {
      if (entry.subscribers.size === 0 || !entry.fetcher) continue;
      entry.errorCount = 0;
      runFetch(entry, key);
    }
  }

  const ns = (window.primerApi = window.primerApi || {});
  ns.useResource = useResource;
  ns._resource = { findKeys, peekData, replaceData, refetchKey };
  ns._refetchAll = refetchAll;
})();
