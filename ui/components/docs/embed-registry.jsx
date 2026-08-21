/* global React */

// ===========================================================================
// Task 1.2 -- embed registry.
//
// Maps every embed id to the real console component global that renders it,
// and to the fixture file stem that provides its data.
//
// USAGE
//   window.DocsEmbedRegistry["agents-page"]
//     => { component: "AgentsPage", fixtures: "agents-page",
//          props: { onOpen: () => {}, pushToast: () => {} } }
//
// window.DocsEmbedIds() returns the canonical id list (in sync with
// primer/user_docs/_fixtures/registry.json written below).
// ===========================================================================

(function () {
  // Each entry:
  //   component  - exact window.<name> the iframe renders
  //   fixtures   - stem of primer/user_docs/_fixtures/<stem>.json
  //   props      - static props to pass when rendering (callbacks are no-ops)
  //
  // Notes on sub-view ids:
  //   session-detail   -> SessionsList (page-level); SessionDetail requires a
  //                       specific sid prop; the sessions-list page naturally
  //                       shows the session from the fixture.
  //   workspace-template-form -> WorkspaceTemplatesPage (the template sub-page
  //                       that includes the create form).
  //   trigger-create   -> TR_TriggersPage (page-level list; TR_CreateTriggerDialog
  //                       is a modal that requires onClose/onCreated and cannot
  //                       render standalone without a parent).
  //   api-token-create -> AT_ApiTokensPage (page-level; AT_CreateTokenDialog is
  //                       a modal that requires onClose/onCreated/onDone).
  //   collection-list  -> CollectionsPage (page-level knowledge list).
  const REGISTRY = {
    "agents-page": {
      component: "AgentsPage",
      fixtures: "agents-page",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "sessions-list": {
      // Sessions live in the workspace shell. The Studio this used to
      // render was deleted on the S8 flag day, so the harness mounted an
      // undefined component and the capture never reached "done".
      //
      // SH_Shell rather than SH_RootGate: the gate resolves auth and
      // picks a workspace, which is boot behaviour the embed does not
      // want, and it takes `wid` exactly as the Studio did. `wid` must
      // match the workspace + session rows in sessions-list.json.
      component: "SH_Shell",
      fixtures: "sessions-list",
      props: { wid: "ws-blogassistant" },
    },
    "session-detail": {
      // The same shell with one session open, so the capture shows the
      // transcript beside the rail. The shell reads the open document
      // from the url rather than from a prop, which is why this carries
      // a hash where the Studio version carried `initialOpen`.
      //
      // The session in session-detail.json is terminal (ended), so the
      // transcript renders fully from GET /sessions/{sid}/messages with
      // no live tap; the harness has no SSE backend.
      component: "SH_Shell",
      fixtures: "session-detail",
      hash: "#/w/ws-blogassistant?doc=session:sess-briefwriter",
      props: { wid: "ws-blogassistant" },
    },
    "workspaces": {
      component: "WorkspacesPage",
      fixtures: "workspaces",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "workspace-template-form": {
      // WorkspaceTemplatesPage is the sub-page component that includes the
      // template create modal; WorkspacesPage embeds it as a tab.
      component: "WorkspaceTemplatesPage",
      fixtures: "workspace-template-form",
      props: { pushToast: function () {} },
    },
    "trigger-create": {
      component: "TR_TriggersPage",
      fixtures: "trigger-create",
      props: {},
    },
    "channels": {
      component: "ChannelsPage",
      fixtures: "channels",
      props: { onNavigate: function () {}, pushToast: function () {} },
    },
    "graph-canvas": {
      // GraphDetail (exposed on window alongside GraphsPage) renders the
      // single-graph editor whose centerpiece is the node/edge canvas
      // (GR_GraphEditor -> GR_Canvas). GraphsPage is the LIST page and never
      // shows the canvas, so the embed mounts the detail directly. graphId
      // must match the GET /graphs/{id} key in the fixture. Node x/y are
      // assigned client-side by primerVendor.autoLayout (server stores none).
      component: "GraphDetail",
      fixtures: "graph-canvas",
      props: { graphId: "docs-producer-judge", pushToast: function () {} },
    },
    "workers-stats": {
      component: "WorkersPage",
      fixtures: "workers-stats",
      props: { sessions: [], pushToast: function () {} },
    },
    "collection-list": {
      component: "CollectionsPage",
      fixtures: "collection-list",
      props: {
        pushToast: function () {},
        onOpen: function () {},
        onNavigate: function () {},
      },
    },
    "api-token-create": {
      component: "AT_ApiTokensPage",
      fixtures: "api-token-create",
      props: {},
    },
    "llm-provider-openrouter": {
      component: "ProviderCatalog",
      fixtures: "llm-provider-openrouter",
      props: { initialClass: "llm", onNavigate: function () {} },
    },
    "quickstart-agents": {
      component: "AgentsPage",
      fixtures: "quickstart-agents",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "internal-collections-enable": {
      component: "InternalCollectionsPage",
      fixtures: "internal-collections-enable",
      props: { pushToast: function () {} },
    },
    "quickstart-graph": {
      component: "GraphsPage",
      fixtures: "quickstart-graph",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "embedding-provider": {
      component: "ProviderCatalog",
      fixtures: "embedding-provider",
      props: { initialClass: "embedding", onNavigate: function () {} },
    },
    "ssp": {
      component: "SSPListPage",
      fixtures: "ssp",
      props: { pushToast: function () {} },
    },
    "cross-encoder-provider": {
      component: "ProviderCatalog",
      fixtures: "cross-encoder-provider",
      props: { initialClass: "cross_encoder", onNavigate: function () {} },
    },
    "web-search": {
      component: "ProviderCatalog",
      fixtures: "web-search",
      props: { initialClass: "web_search", onNavigate: function () {} },
    },
    "workspace-provider-create": {
      // The workspace providers list/create lives in WorkspaceProvidersPage
      // (ui/components/workspaces/providers.jsx); WorkspacesPage embeds it.
      component: "WorkspaceProvidersPage",
      fixtures: "workspace-provider-create",
      props: { pushToast: function () {} },
    },
    "channel-provider-create": {
      component: "ChannelProvidersPage",
      fixtures: "channel-provider-create",
      props: { onNavigate: function () {}, pushToast: function () {} },
    },
    "harness": {
      component: "HarnessesPage",
      fixtures: "harness",
      props: { pushToast: function () {} },
    },
    "mcp-exposure": {
      component: "MC_McpPage",
      fixtures: "mcp-exposure",
      props: { pushToast: function () {} },
    },
    "approvals": {
      component: "ApprovalsPage",
      fixtures: "approvals",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "toolsets": {
      component: "ToolsetsPage",
      fixtures: "toolsets",
      props: { onOpen: function () {}, pushToast: function () {} },
    },
    "collection-create": {
      component: "CollectionsPage",
      fixtures: "collection-create",
      props: {
        pushToast: function () {},
        onOpen: function () {},
        onNavigate: function () {},
      },
    },
  };

  window.DocsEmbedRegistry = REGISTRY;

  window.DocsEmbedIds = function () {
    return Object.keys(REGISTRY);
  };
})();
