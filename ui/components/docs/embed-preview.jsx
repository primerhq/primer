/* global React */

// ===========================================================================
// SPIKE (Task 1.1) — fixture-backed primerApi stub for live component embeds.
//
// PURPOSE
//   The user-docs revamp embeds REAL console page components (window.AgentsPage,
//   window.ChatsPage, ...) inside documentation pages, rendered against frozen
//   fixture data instead of the live API. This file provides the stub that
//   feeds those components their data, plus the helper that mounts a real
//   component under that stub.
//
// THE DATA SEAM
//   Every page component reads data through exactly one global namespace:
//     window.primerApi.{ apiFetch, useResource, useMutation, useRouter,
//                        useViewport }
//   A component destructures it at the top of its render, e.g. agents.jsx:
//     const { useResource, useRouter, useViewport, apiFetch } = window.primerApi;
//   So to render a component against fixtures we must make THAT global resolve
//   to a stub for the component's subtree.
//
// ===========================================================================
// LOCKED ISOLATION MECHANISM: (B) IFRAME  — and WHY.
// ===========================================================================
//   `window.primerApi` is a single mutable global shared by the entire app.
//   Candidate (A) "in-page": temporarily swap window.primerApi to the stub
//   for the embed's render and restore it after. REJECTED: React renders and
//   effects fire ASYNCHRONOUSLY (useResource's fetcher runs in an effect, the
//   per-row status/session effects fire after paint). A swap-and-restore is a
//   race — by the time those effects run the real api is back, so the embed
//   either hits the network or sees the wrong api; worse, during the swap the
//   LIVE app's own components (Dashboard, sidebar polls) read the stub. The
//   "safe (A)" variant (React context + every component reading api via a small
//   indirection) would require touching every page component, which defeats the
//   "reuse the REAL component unchanged" goal of the whole plan.
//
//   Candidate (B) "iframe": render the component inside an <iframe> whose OWN
//   window.primerApi IS the stub. CHOSEN. True isolation of the global, of
//   React state, and of styles. The component runs unmodified; nothing leaks
//   into or out of the host docs app. No per-component refactor. The only cost
//   is one iframe per embed, which is acceptable for documentation.
//
// PER-COMPONENT REQUIREMENTS THE REST OF THE EMBEDS WILL NEED
//   1. The stub must answer EVERY apiFetch the component makes during render +
//      effects. Unknown "<METHOD> <path>" falls back to an empty offset page so
//      a component that lists something never crashes; if a component needs a
//      specific shape (a single object, a non-empty list) the fixture must
//      provide it keyed by "<METHOD> <path>".
//   2. The stub provides useRouter()/useViewport() shims too, because page
//      components call them. navigate() is a no-op inside the doc.
//   3. The iframe must load the real /console/_app.js (gives React + the design
//      system globals Icon/Btn/Card/CardList/... + styles + the component) and
//      then install the stub as window.primerApi BEFORE the component renders.
//      The full-app auto-boot is suppressed by NOT giving the iframe a #root.
// ===========================================================================

(function () {
  const EMPTY_OFFSET_PAGE = Object.freeze({
    kind: "offset",
    items: [],
    total: 0,
    offset: 0,
    length: 0,
  });

  // Strip the query string from a "<METHOD> <path>" key, for a
  // query-insensitive fallback lookup.
  function _stripQuery(key) {
    const q = key.indexOf("?");
    return q === -1 ? key : key.slice(0, q);
  }

  // Build a fixture-backed primerApi. `fixtures` is an object keyed by
  // "<METHOD> <path>" (exactly the shape of
  // primer/user_docs/_fixtures/<name>.json), e.g.
  //   { "GET /agents?limit=200&offset=0": { kind:"offset", items:[...], ... } }
  //
  // The returned object is a drop-in for window.primerApi: it exposes the
  // same { apiFetch, useResource, useMutation, useRouter, useViewport }
  // surface the page components destructure.
  function DocsMakeStubApi(fixtures) {
    const table = fixtures || {};

    // Resolve a fixture by "<METHOD> <path>". Exact match first, then a
    // query-insensitive match (so a fixture keyed without the query, or with
    // a different query string, still answers), else an empty offset page so
    // list components never crash.
    function _resolve(method, path) {
      const upper = String(method || "GET").toUpperCase();
      const key = upper + " " + String(path);
      if (Object.prototype.hasOwnProperty.call(table, key)) return table[key];

      const bare = _stripQuery(key);
      for (const k of Object.keys(table)) {
        if (_stripQuery(k) === bare) return table[k];
      }
      return EMPTY_OFFSET_PAGE;
    }

    // apiFetch(method, path, body, opts) — always resolves from fixtures,
    // never touches the network. Async to match the real signature so
    // `await apiFetch(...)` and `.then(...)` both work.
    async function apiFetch(method, path /* , body, opts */) {
      return _resolve(method, path);
    }

    // useResource(key, fetcher, opts) — run the fetcher exactly once and
    // expose its result. No polling, no abort, no error path (fixtures never
    // fail). Mirrors the real hook's { data, loading, error, refetch } shape.
    function useResource(_cacheKey, fetcher /* , opts */) {
      const [state, setState] = React.useState({ data: null, loading: true });
      React.useEffect(() => {
        let alive = true;
        Promise.resolve(fetcher(undefined))
          .then((data) => { if (alive) setState({ data, loading: false }); })
          .catch(() => { if (alive) setState({ data: null, loading: false }); });
        return () => { alive = false; };
        // Run once: the fetcher closes over apiFetch which is stable.
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, []);
      return {
        data: state.data,
        loading: state.loading,
        error: null,
        refetch: () => {},
      };
    }

    // useMutation(...) — no-op. Docs embeds are read-only previews; a click on
    // a "create"/"delete" control must not mutate anything. Returns the real
    // hook's { run, loading, error, reset } surface with inert implementations.
    function useMutation(/* fetcher, options */) {
      return {
        run: async () => undefined,
        loading: false,
        error: null,
        reset: () => {},
      };
    }

    // Page components call useRouter()/useViewport(). Inside a doc there is no
    // hash router and no responsive app shell, so navigate() is inert and the
    // viewport is reported as desktop (the embed host sizes the iframe).
    function useRouter() {
      return {
        path: "/",
        params: {},
        query: {},
        navigate: () => {},
      };
    }

    function useViewport() {
      return { isMobile: false, width: 1366, height: 768 };
    }

    return {
      apiFetch,
      useResource,
      useMutation,
      useRouter,
      useViewport,
      // Marker so debuggers / tests can confirm the stub (not the live api)
      // is in force inside an embed iframe.
      __isDocsStub: true,
    };
  }

  window.DocsMakeStubApi = DocsMakeStubApi;
})();
