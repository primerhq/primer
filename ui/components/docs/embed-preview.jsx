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

    // usePagedList(...) — the paginated list pages (agents, graphs, chats…)
    // now fetch through this instead of useResource. In a read-only docs
    // preview there is one fixture page, so fetch page 0 via the stubbed
    // apiFetch and report a single, non-navigable page. Mirrors the real
    // hook's return surface so the page component + <Pager> render.
    function usePagedList(opts) {
      const path = (opts && opts.path) || "";
      const pageSize = (opts && opts.pageSize) || 50;
      const res = useResource(
        "docs-stub:" + path,
        () =>
          apiFetch(
            "GET",
            path + (path.indexOf("?") >= 0 ? "&" : "?") +
              "limit=" + pageSize + "&offset=0",
          ),
      );
      const data = res.data || {};
      const items = data.items || [];
      return {
        items: items,
        total: data.total != null ? data.total : items.length,
        data: res.data,
        loading: res.loading,
        error: null,
        refetch: () => {},
        offset: 0,
        pageSize: pageSize,
        page: 0,
        hasNext: false,
        hasPrev: false,
        rangeStart: items.length ? 1 : 0,
        rangeEnd: items.length,
        next: () => {},
        prev: () => {},
        reset: () => {},
        setOffset: () => {},
      };
    }

    // Pager(...) — inert in a docs preview (one fixture page, no navigation).
    function Pager() {
      return null;
    }

    return {
      apiFetch,
      useResource,
      useMutation,
      useRouter,
      useViewport,
      usePagedList,
      Pager,
      // Marker so debuggers / tests can confirm the stub (not the live api)
      // is in force inside an embed iframe.
      __isDocsStub: true,
    };
  }

  window.DocsMakeStubApi = DocsMakeStubApi;

  // ===========================================================================
  // DocsBootEmbedIframe(iframe, componentName, fixturesOrPath, props, onDone)
  //
  // Shared iframe-boot helper used by the embed: directive. Calls back
  // onDone(err) when the component has been rendered (err=null) or a fatal
  // error occurred (err=Error).
  //
  // Parameters:
  //   iframe          - the <iframe> DOM element (must already be in the DOM)
  //   componentName   - exact window.<name> string, e.g. "AgentsPage"
  //   fixturesOrPath  - EITHER a URL string to fetch as JSON (e.g.
  //                     "/v1/user_docs/_fixtures/agents-page.json"),
  //                     OR a plain object containing the fixtures inline.
  //                     Inline objects are used by test harnesses so auth is
  //                     not required.
  //   props           - plain object of static props to pass to the component
  //   onDone          - function(err) called once; err is null on success
  // ===========================================================================
  function DocsBootEmbedIframe(iframe, componentName, fixturesOrPath, props, onDone) {
    var idoc = iframe.contentDocument;

    // Build the iframe document with no #root so the full-app auto-boot
    // (ReactDOM.createRoot(getElementById("root")).render) throws harmlessly;
    // every component global is already registered by that point.
    idoc.open();
    idoc.write(
      "<!doctype html>" +
      "<html lang=\"en\" data-theme=\"dark\"><head>" +
      "<meta charset=\"utf-8\" />" +
      "<link rel=\"stylesheet\" href=\"/console/styles.css\" />" +
      "<style>html,body{margin:0;background:var(--bg);} #embed-root{padding:16px;}</style>" +
      "</head><body>" +
      "<div id=\"embed-root\"></div>" +
      "<script src=\"/console/vendor/react.min.js\"><\/script>" +
      "<script src=\"/console/vendor/react-dom.min.js\"><\/script>" +
      "<script src=\"/console/vendor/html2canvas.min.js\"><\/script>" +
      "<\/body><\/html>"
    );
    idoc.close();

    var iwin = iframe.contentWindow;

    // Swallow the harmless createRoot(null) error produced when the bundle's
    // auto-boot runs inside the frame (there is no #root).
    iwin.addEventListener("error", function (e) {
      var msg = String((e && e.message) || "");
      if (/root|createRoot|null/i.test(msg)) { e.preventDefault(); }
    });

    // Load a plain-JS or JSX source file into the iframe by fetching it and
    // injecting an inline <script>. The static handler serves .jsx as
    // text/plain with nosniff so <script src> is refused; fetch+inline
    // sidesteps the MIME guard.
    function loadSourceInline(src) {
      return iwin.fetch(src).then(function (res) {
        if (!res.ok) throw new Error("failed to fetch " + src + " (" + res.status + ")");
        return res.text();
      }).then(function (code) {
        var s = idoc.createElement("script");
        s.textContent = code;
        idoc.body.appendChild(s);
      });
    }

    // Wait for React + ReactDOM to be available in the iframe.
    function waitForReact() {
      return new Promise(function (resolve) {
        var t = setInterval(function () {
          if (iwin.React && iwin.ReactDOM) { clearInterval(t); resolve(); }
        }, 10);
      });
    }

    waitForReact()
      .then(function () { return loadSourceInline("/console/_app.js"); })
      .then(function () { return loadSourceInline("/console/components/docs/embed-preview.jsx"); })
      .then(function () {
        if (typeof iwin[componentName] !== "function") {
          throw new Error(componentName + " not defined in iframe after bundle load");
        }
        if (typeof iwin.DocsMakeStubApi !== "function") {
          throw new Error("DocsMakeStubApi not defined in iframe");
        }
        // Resolve fixtures: accept either a URL string (fetch from server)
        // or an inline plain object (used by test harnesses).
        if (fixturesOrPath && typeof fixturesOrPath === "object") {
          return Promise.resolve(fixturesOrPath);
        }
        return iwin.fetch(fixturesOrPath).then(function (res) {
          if (!res.ok) throw new Error("fixtures fetch failed: " + res.status);
          return res.json();
        });
      })
      .then(function (fixtures) {
        iwin.primerApi = iwin.DocsMakeStubApi(fixtures);
        // Render the real component into the embed mount.
        var root = iwin.ReactDOM.createRoot(idoc.getElementById("embed-root"));
        root.render(iwin.React.createElement(iwin[componentName], props || {}));
        onDone(null);
      })
      .catch(function (err) {
        onDone(err);
      });
  }

  window.DocsBootEmbedIframe = DocsBootEmbedIframe;
})();
