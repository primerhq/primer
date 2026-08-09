/* global React */
// Studio2 document registry: every open thing is a {kind, ref} tab.
// Contract from spec section 5. State lives outside React; components
// subscribe via useDocsVersion().
(function () {
  const kinds = new Map();
  let tabs = [];
  let active = null;
  const dirty = new Set();
  let version = 0;
  const listeners = new Set();

  function bump() {
    version += 1;
    persist();
    listeners.forEach((fn) => fn());
  }
  function persist() {
    try {
      localStorage.setItem("studio2:tabs", JSON.stringify({
        tabs: tabs.map((t) => ({ kind: t.kind, ref: t.ref })),
        active,
      }));
    } catch (_e) { /* storage denied: tabs just do not persist */ }
  }
  function keyOf(kind, ref) { return kind + ":" + ref; }
  function mirror() {
    if (!active) return;
    const kind = active.slice(0, active.indexOf(":"));
    const ref = active.slice(active.indexOf(":") + 1);
    window.location.hash = "#/studio2?open=" + kind + ":" + encodeURIComponent(ref);
  }

  window.S2_Docs = {
    registerKind(kind, def) { kinds.set(kind, def); },
    kindDef(kind) { return kinds.get(kind); },
    open(kind, ref) {
      const key = keyOf(kind, ref);
      if (!tabs.some((t) => t.key === key)) {
        tabs.push({ key, kind, ref });
      }
      active = key;
      mirror();
      bump();
    },
    activate(key) {
      if (tabs.some((t) => t.key === key)) { active = key; mirror(); bump(); }
    },
    close(key) {
      const t = tabs.find((x) => x.key === key);
      if (!t) return;
      if (dirty.has(key) &&
          !window.confirm("This document has unsaved changes. Close anyway?")) return;
      dirty.delete(key);
      const i = tabs.indexOf(t);
      tabs = tabs.filter((x) => x.key !== key);
      if (active === key) active = tabs.length ? tabs[Math.max(0, i - 1)].key : null;
      if (active) mirror();
      bump();
    },
    setDirty(key, isDirty) {
      if (isDirty) dirty.add(key); else dirty.delete(key);
      bump();
    },
    isDirty(key) { return dirty.has(key); },
    tabs() {
      return tabs.map((t) => {
        const def = kinds.get(t.kind) || {};
        return {
          ...t,
          dirty: dirty.has(t.key),
          glyph: def.glyph || "▢",
          title: def.title ? def.title(t.ref) : t.ref,
        };
      });
    },
    activeKey() { return active; },
    restore() {
      try {
        const saved = JSON.parse(localStorage.getItem("studio2:tabs") || "null");
        if (saved && Array.isArray(saved.tabs)) {
          tabs = saved.tabs
            .filter((t) => kinds.has(t.kind))
            .map((t) => ({ key: keyOf(t.kind, t.ref), kind: t.kind, ref: t.ref }));
          active = tabs.some((t) => t.key === saved.active)
            ? saved.active
            : (tabs[0] || {}).key || null;
        }
      } catch (_e) { /* corrupt state: start empty */ }
      // The ?open= deep link wins over the stored active tab.
      const m = window.location.hash.match(/[?&]open=([a-z0-9_-]+):([^&]+)/i);
      if (m && kinds.has(m[1])) {
        this.open(m[1], decodeURIComponent(m[2]));
        return;
      }
      bump();
    },
    useDocsVersion() {
      const [, setV] = React.useState(0);
      React.useEffect(() => {
        const fn = () => setV((x) => x + 1);
        listeners.add(fn);
        return () => listeners.delete(fn);
      }, []);
      return version;
    },
  };

  window.S2_Commands.register({
    id: "tab:close", title: "Close tab", glyph: "⌘", cat: "view",
    shortcut: (window.S2_MOD || "Ctrl") + " W",
    run: () => {
      const k = window.S2_Docs.activeKey();
      if (k) window.S2_Docs.close(k);
    },
  });
  for (let n = 1; n <= 9; n += 1) {
    window.S2_Commands.register({
      id: "tab:" + n, title: "Go to tab " + n, glyph: "⌘", cat: "view",
      run: () => {
        const t = window.S2_Docs.tabs()[n - 1];
        if (t) window.S2_Docs.activate(t.key);
      },
    });
  }
})();
