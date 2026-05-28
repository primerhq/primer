// primer UI — useMutation hook (POST/PUT/DELETE wrapper with optimistic
// updates, cache invalidation, and 7807 error fallback).
// Loaded via <script type="text/babel"> in ui/index.html. Depends on
// React (global) and the internal `window.primerApi._resource` cache
// helpers exposed by use-resource.js. Optionally calls
// `window.primerApi.toastPush` (provided by toast.js when present).

(function () {
  const { useState, useRef, useCallback } = window.React;

  function useMutation(fetcher, options = {}) {
    const [state, setState] = useState({
      loading: false,
      error: null,
      data: undefined,
    });

    const fetcherRef = useRef(fetcher);
    fetcherRef.current = fetcher;
    const optsRef = useRef(options);
    optsRef.current = options;

    const mutate = useCallback(async (body) => {
      const opts = optsRef.current || {};
      const ns = window.primerApi || {};
      const resourceApi = ns._resource || null;
      const invalidates = Array.isArray(opts.invalidates) ? opts.invalidates : [];
      const optimistic =
        typeof opts.optimistic === "function" ? opts.optimistic : null;

      // Snapshot affected caches and apply optimistic transform.
      const snapshots = [];
      if (optimistic && resourceApi) {
        for (const baseKey of invalidates) {
          for (const key of resourceApi.findKeys(baseKey)) {
            const current = resourceApi.peekData(key);
            snapshots.push({ key, data: current });
            resourceApi.replaceData(key, optimistic(current, body));
          }
        }
      }

      setState({ loading: true, error: null, data: undefined });

      try {
        const data = await fetcherRef.current(body);
        setState({ loading: false, error: null, data });
        if (resourceApi) {
          for (const baseKey of invalidates) {
            for (const key of resourceApi.findKeys(baseKey)) {
              resourceApi.refetchKey(key);
            }
          }
        }
        if (typeof opts.onSuccess === "function") opts.onSuccess(data);
        return data;
      } catch (err) {
        setState({ loading: false, error: err, data: undefined });
        // Rollback optimistic, then refetch to converge with server truth.
        if (resourceApi) {
          for (const s of snapshots) resourceApi.replaceData(s.key, s.data);
          for (const baseKey of invalidates) {
            for (const key of resourceApi.findKeys(baseKey)) {
              resourceApi.refetchKey(key);
            }
          }
        }
        if (typeof opts.onError === "function") {
          opts.onError(err);
        } else {
          const push = ns.toastPush;
          if (typeof push === "function") {
            push({
              kind: "error",
              title: (err && err.title) || "Request failed",
              detail: (err && (err.detail || err.message)) || "",
              requestId: (err && err.requestId) || null,
            });
          }
        }
        throw err;
      }
    }, []);

    return {
      mutate,
      loading: state.loading,
      error: state.error,
      data: state.data,
    };
  }

  const ns = (window.primerApi = window.primerApi || {});
  ns.useMutation = useMutation;
})();
