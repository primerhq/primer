// matrix UI — useRouter hook (hash router, ~50 LoC of routing logic).
// Loaded via <script type="text/babel"> in ui/index.html. Depends on
// React (global). Routes table is mutable: other sub-projects may push
// additional entries via window.matrixApi.routes.

(function () {
  const { useState, useEffect, useCallback } = window.React;

  // Initial route table — from parent spec §9.1. Order matters because
  // resolveRoute() is first-match-wins and segment-count equality is the
  // only ambiguity guard; any literal route that shares segment count
  // with a parameterised sibling MUST appear earlier in the list (e.g.
  // "/toolsets/builtin" before "/toolsets/:id").
  const routes = [
    { pattern: "/",                                page: "DashboardPage" },
    { pattern: "/sessions",                        page: "SessionsListPage" },
    { pattern: "/sessions/:id",                    page: "SessionDetailPage" },
    { pattern: "/workspaces",                      page: "WorkspacesListPage" },
    { pattern: "/workspaces/:id",                  page: "WorkspaceDetailPage" },
    { pattern: "/workspaces/:id/:tab",             page: "WorkspaceDetailPage" },
    { pattern: "/agents",                          page: "AgentsListPage" },
    { pattern: "/agents/:id",                      page: "AgentDetailPage" },
    { pattern: "/graphs",                          page: "GraphsListPage" },
    { pattern: "/graphs/:id",                      page: "GraphDetailPage" },
    { pattern: "/knowledge/collections",           page: "CollectionsListPage" },
    { pattern: "/knowledge/collections/:id",       page: "CollectionDetailPage" },
    { pattern: "/knowledge/documents",             page: "DocumentsListPage" },
    { pattern: "/knowledge/documents/:id",         page: "DocumentDetailPage" },
    { pattern: "/knowledge/search",                page: "SearchBenchPage" },
    { pattern: "/toolsets",                        page: "ToolsetsListPage" },
    { pattern: "/toolsets/builtin",                page: "BuiltinToolsetsPage" },
    { pattern: "/toolsets/:id",                    page: "ToolsetDetailPage" },
    { pattern: "/providers/llm",                   page: "LlmProvidersListPage" },
    { pattern: "/providers/llm/:id",               page: "LlmProviderDetailPage" },
    { pattern: "/providers/embedding",             page: "EmbeddingProvidersListPage" },
    { pattern: "/providers/embedding/:id",         page: "EmbeddingProviderDetailPage" },
    { pattern: "/providers/cross_encoder",         page: "CrossEncoderProvidersListPage" },
    { pattern: "/providers/cross_encoder/:id",     page: "CrossEncoderProviderDetailPage" },
    { pattern: "/subsystems/internal-collections", page: "InternalCollectionsPage" },
    { pattern: "/workers",                         page: "WorkersPage" },
    { pattern: "/health",                          page: "HealthPage" },
    // Phase 1 additions for redesigned console pages.
    { pattern: "/ssp",                             page: "SemanticSearchListPage" },
    { pattern: "/ssp/:id",                         page: "SemanticSearchDetailPage" },
    { pattern: "/approvals",                       page: "ApprovalsPage" },
    { pattern: "/channels/providers",              page: "ChannelProvidersPage" },
    { pattern: "/channels/providers/:id",          page: "ChannelProviderDetailPage" },
    { pattern: "/channels/channels",               page: "ChannelsListPage" },
    { pattern: "/channels/associations",           page: "ChannelAssociationsPage" },
    { pattern: "/chats",                           page: "ChatsListPage" },
    { pattern: "/chats/:id",                       page: "ChatDetailPage" },
  ];

  function splitSegments(path) {
    if (!path || path === "/") return [""];
    const trimmed = path.startsWith("/") ? path.slice(1) : path;
    return trimmed.split("/");
  }

  function matchPattern(pattern, path) {
    const ps = splitSegments(pattern);
    const xs = splitSegments(path);
    if (ps.length !== xs.length) return null;
    const params = {};
    for (let i = 0; i < ps.length; i++) {
      if (ps[i].startsWith(":")) {
        try {
          params[ps[i].slice(1)] = decodeURIComponent(xs[i]);
        } catch (_e) {
          params[ps[i].slice(1)] = xs[i];
        }
      } else if (ps[i] !== xs[i]) {
        return null;
      }
    }
    return params;
  }

  function resolveRoute(path) {
    for (const r of routes) {
      const params = matchPattern(r.pattern, path);
      if (params) return { route: r, params };
    }
    return null;
  }

  function parseHash(rawHash) {
    const raw = rawHash || "";
    const stripped = raw.startsWith("#") ? raw.slice(1) : raw;
    const qIdx = stripped.indexOf("?");
    let path = qIdx >= 0 ? stripped.slice(0, qIdx) : stripped;
    const queryStr = qIdx >= 0 ? stripped.slice(qIdx + 1) : "";
    if (!path) path = "/";
    const query = {};
    if (queryStr) {
      const usp = new URLSearchParams(queryStr);
      usp.forEach((v, k) => {
        query[k] = v;
      });
    }
    return { path, query };
  }

  function buildHash(path, query) {
    let s = "#" + path;
    if (query && typeof query === "object") {
      const usp = new URLSearchParams();
      for (const [k, v] of Object.entries(query)) {
        if (v != null) usp.append(k, String(v));
      }
      const q = usp.toString();
      if (q) s += "?" + q;
    }
    return s;
  }

  function navigate(path, query) {
    window.location.hash = buildHash(path, query);
  }

  function useRouter() {
    const [hash, setHash] = useState(() => window.location.hash || "#/");

    useEffect(() => {
      const onHashChange = () => setHash(window.location.hash || "#/");
      window.addEventListener("hashchange", onHashChange);
      // Spec §9.1: missing hash redirects to "#/".
      if (!window.location.hash) window.location.hash = "#/";
      return () => window.removeEventListener("hashchange", onHashChange);
    }, []);

    const parsed = parseHash(hash);
    const matched = resolveRoute(parsed.path);
    const navCb = useCallback(navigate, []);

    return {
      path: matched ? parsed.path : "/__notfound__",
      params: matched ? matched.params : {},
      query: parsed.query,
      navigate: navCb,
    };
  }

  const ns = (window.matrixApi = window.matrixApi || {});
  ns.useRouter = useRouter;
  ns.routes = routes;
  // Helpers exposed for app.jsx (page resolution) and tests.
  ns.matchRoute = matchPattern;
  ns._router = { parseHash, matchPattern, resolveRoute, buildHash };
})();
