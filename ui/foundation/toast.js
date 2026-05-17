// matrix UI — useToast hook (toast queue, FIFO, max 5 visible).
// Loaded via <script type="text/babel"> in ui/index.html. Depends on
// React (global). Also exposes module-level push/dismiss so non-React
// code (e.g. useMutation's onError fallback) can enqueue toasts without
// being inside a component.

(function () {
  const { useState, useEffect } = window.React;

  // Per spec §6: success 5s, info 5s, warning 8s, error 10s, sticky never.
  const DEFAULT_DURATION_MS = {
    success: 5000,
    info: 5000,
    warning: 8000,
    error: 10000,
    sticky: 0,
  };
  const MAX_VISIBLE = 5;

  const state = {
    toasts: [],
    listeners: new Set(),
  };
  let nextId = 1;
  const timers = new Map(); // id -> setTimeout handle

  function notify() {
    const snapshot = state.toasts.slice();
    for (const listener of state.listeners) listener(snapshot);
  }

  function clearTimer(id) {
    const handle = timers.get(id);
    if (handle != null) {
      clearTimeout(handle);
      timers.delete(id);
    }
  }

  function dismiss(id) {
    const idx = state.toasts.findIndex((t) => t.id === id);
    if (idx === -1) return;
    state.toasts.splice(idx, 1);
    clearTimer(id);
    notify();
  }

  function push(toast) {
    const t = toast || {};
    const id = nextId++;
    const kind = t.kind || "info";
    const defaultMs =
      DEFAULT_DURATION_MS[kind] != null
        ? DEFAULT_DURATION_MS[kind]
        : DEFAULT_DURATION_MS.info;
    const durationMs =
      typeof t.durationMs === "number" ? t.durationMs : defaultMs;
    const entry = {
      id,
      kind,
      title: t.title || "",
      detail: t.detail || "",
      requestId: t.requestId || null,
      actions: Array.isArray(t.actions) ? t.actions : [],
      durationMs,
    };
    state.toasts.push(entry);
    // Enforce MAX_VISIBLE — drop oldest (FIFO).
    while (state.toasts.length > MAX_VISIBLE) {
      const dropped = state.toasts.shift();
      clearTimer(dropped.id);
    }
    if (durationMs > 0) {
      const handle = setTimeout(() => dismiss(id), durationMs);
      timers.set(id, handle);
    }
    notify();
    return id;
  }

  function useToast() {
    const [toasts, setToasts] = useState(() => state.toasts.slice());
    useEffect(() => {
      const listener = (snap) => setToasts(snap);
      state.listeners.add(listener);
      // Sync to current state at mount in case toasts were pushed before
      // this subscriber attached (e.g. early-page error).
      setToasts(state.toasts.slice());
      return () => {
        state.listeners.delete(listener);
      };
    }, []);
    return { toasts, push, dismiss };
  }

  const ns = (window.matrixApi = window.matrixApi || {});
  ns.useToast = useToast;
  // Non-hook entry points for code that lives outside the React tree.
  ns.toastPush = push;
  ns.toastDismiss = dismiss;
  // For Task 14 foundation tests.
  ns._toast = { state, push, dismiss };
})();
